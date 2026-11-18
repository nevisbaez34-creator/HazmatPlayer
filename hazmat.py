#!/usr/bin/env python3
"""
HazmatPlayer CLI v6.6.0 MFULL - Interactive Multimedia Shell & Processor
Format-agnostic multimedia CLI with non-blocking playback and live '--' interactive console.
© Quail Software 2026
"""

import argparse
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional


class HazmatPlayerError(Exception):
    """Custom exception for HazmatPlayer CLI."""
    def __init__(self, code: str, message: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.code = code
        self.cause = cause


class HazmatPlayerCLI:
    VERSION = "6.6.0 Hazmat"
    EQ_FREQUENCIES = [60, 230, 910, 3600, 14000]

    def __init__(self, ffmpeg_bin: Optional[str] = None, ffplay_bin: Optional[str] = None, ffprobe_bin: Optional[str] = None):
        self.ffmpeg = self._resolve_binary(ffmpeg_bin, "ffmpeg")
        self.ffplay = self._resolve_binary(ffplay_bin, "ffplay")
        self.ffprobe = self._resolve_binary(ffprobe_bin, "ffprobe")

        self.playlist: List[str] = []
        self.current_index: int = -1
        self.repeat_mode: str = "off"  # off | one | all
        self.is_paused: bool = False
        self.is_muted: bool = False
        self.volume: int = 100
        self.extra_volume: float = 1.0
        self.eq_gains: Optional[List[float]] = None
        self.rate: float = 1.0
        self.link_pitch: bool = False
        self.subtitle: Optional[str] = None

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stop_requested = False
        self._manual_jump = False

    @staticmethod
    def _resolve_binary(custom_path: Optional[str], name: str) -> str:
        if custom_path and os.path.isfile(custom_path):
            return custom_path
        found = shutil.which(name)
        if found:
            return found
        win_default = Path(f"{name}.exe")
        if win_default.is_file():
            return str(win_default)
        return name

    def detect_category(self, source: str) -> str:
        s = source.lower()
        if ".m3u8" in s:
            return "hls"
        if ".mpd" in s:
            return "dash"
        if s.startswith(("rtmp://", "rtsp://", "fsv://")):
            return "rtmp_rtsp"

        guessed_mime, _ = mimetypes.guess_type(source)
        if guessed_mime:
            if guessed_mime.startswith("image/"):
                return "image"
            if guessed_mime.startswith("audio/"):
                return "audio"
            if guessed_mime.startswith("video/"):
                return "video"

        try:
            cmd = [
                self.ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                source,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                streams = data.get("streams", [])
                codec_types = {st.get("codec_type") for st in streams}
                format_name = (data.get("format", {}).get("format_name") or "").lower()
                if "video" in codec_types:
                    if "image2" in format_name or format_name.endswith("_pipe"):
                        return "image"
                    return "video"
                if "audio" in codec_types:
                    return "audio"
        except Exception:
            pass

        return "media"

    def _build_audio_filters(self) -> List[str]:
        filters = []
        if self.eq_gains:
            for idx, gain in enumerate(self.eq_gains[:5]):
                if gain != 0:
                    freq = self.EQ_FREQUENCIES[idx]
                    filters.append(f"equalizer=f={freq}:t=q:w=1.0:g={float(gain)}")

        clamped_vol = max(1.0, min(3.0, float(self.extra_volume)))
        if clamped_vol != 1.0:
            filters.append(f"volume={clamped_vol}")

        rate = float(self.rate)
        if rate != 1.0:
            if self.link_pitch:
                filters.append(f"asetrate=44100*{rate},aresample=44100")
            else:
                remaining = rate
                while remaining > 2.0:
                    filters.append("atempo=2.0")
                    remaining /= 2.0
                while remaining < 0.5:
                    filters.append("atempo=0.5")
                    remaining /= 0.5
                filters.append(f"atempo={remaining:.4f}")

        return filters

    def _build_video_filters(self) -> List[str]:
        filters = []
        rate = float(self.rate)
        if rate != 1.0:
            filters.append(f"setpts={1.0 / rate:.6f}*PTS")
        if self.subtitle:
            escaped_sub = self.subtitle.replace("\\", "/").replace(":", "\\:")
            filters.append(f"subtitles='{escaped_sub}'")
        return filters

    # --- Process & Pause Control (Windows / POSIX) ---

    def _suspend_resume_process(self, suspend: bool) -> bool:
        if not self._proc or self._proc.poll() is not None:
            return False

        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_ALL_ACCESS = 0x1F0FFF
                ntdll = ctypes.windll.ntdll
                handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, self._proc.pid)
                if not handle:
                    return False
                try:
                    if suspend:
                        ntdll.NtSuspendProcess(handle)
                    else:
                        ntdll.NtResumeProcess(handle)
                    return True
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return False
        else:
            import signal
            try:
                os.kill(self._proc.pid, signal.SIGSTOP if suspend else signal.SIGCONT)
                return True
            except Exception:
                return False

    def stop_current(self, user_initiated: bool = True) -> None:
        with self._lock:
            if user_initiated:
                self._stop_requested = True
            if self._proc and self._proc.poll() is None:
                if self.is_paused:
                    self._suspend_resume_process(False)
                    self.is_paused = False
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None

    def toggle_pause(self) -> None:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                print("[HazmatPlayer] No active playback to pause/resume.")
                return
            target_state = not self.is_paused
            if self._suspend_resume_process(target_state):
                self.is_paused = target_state
                state_label = "PAUSED" if self.is_paused else "RESUMED"
                print(f"[HazmatPlayer] Playback {state_label}.")
            else:
                print("[HazmatPlayer] Unable to toggle pause on current process.")

    # --- Playlist & Non-Blocking Playback ---

    def add_to_playlist(self, items: List[str]) -> None:
        count = 0
        for item in items:
            cleaned = item.strip().strip('"').strip("'")
            if cleaned:
                self.playlist.append(cleaned)
                count += 1
        print(f"[HazmatPlayer] Added {count} item(s). Playlist size: {len(self.playlist)}")

    def clear_playlist(self) -> None:
        self.stop_current(user_initiated=True)
        self.playlist.clear()
        self.current_index = -1
        print("[HazmatPlayer] Playlist cleared.")

    def show_playlist(self) -> None:
        if not self.playlist:
            print("[HazmatPlayer] Playlist is empty.")
            return
        print(f"--- HazmatPlayer Playlist (repeat: {self.repeat_mode}) ---")
        for idx, item in enumerate(self.playlist):
            marker = ">" if idx == self.current_index else " "
            print(f"  {marker} [{idx}] {item}")

    def _spawn_player(self, source: str, seek: float = 0.0) -> None:
        category = self.detect_category(source)
        cmd = [
            self.ffplay,
            "-autoexit",
            "-loglevel", "quiet",
            "-window_title", f"HazmatPlayer v{self.VERSION} [{category.upper()}]",
        ]

        if self.is_muted:
            cmd += ["-an"]
        else:
            cmd += ["-volume", str(max(0, min(100, int(self.volume))))]

        if seek > 0:
            cmd += ["-ss", str(seek)]

        af = self._build_audio_filters()
        vf = self._build_video_filters()

        if af and not self.is_muted:
            cmd += ["-af", ",".join(af)]
        if vf and category != "audio":
            cmd += ["-vf", ",".join(vf)]

        cmd.append(source)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.is_paused = False
        except FileNotFoundError as exc:
            raise HazmatPlayerError("BINARY_NOT_FOUND", f"[HazmatPlayer] Executable not found: {self.ffplay}", exc)

    def _playback_worker(self, start_index: int, seek: float = 0.0) -> None:
        self.current_index = start_index
        current_seek = seek

        while 0 <= self.current_index < len(self.playlist):
            with self._lock:
                self._stop_requested = False
                self._manual_jump = False
                source = self.playlist[self.current_index]
                print(f"\n[HazmatPlayer] Now playing [{self.current_index}]: {source}")
                self._spawn_player(source, seek=current_seek)
                current_seek = 0.0

            while True:
                proc = self._proc
                if proc is None:
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.15)

            with self._lock:
                if self._stop_requested:
                    break
                if self._manual_jump:
                    continue

                if self.repeat_mode == "one":
                    continue
                elif self.current_index + 1 < len(self.playlist):
                    self.current_index += 1
                elif self.repeat_mode == "all":
                    self.current_index = 0
                else:
                    self.current_index = -1
                    print("\n[HazmatPlayer] Playlist finished.")
                    break

    def play_index(self, index: int, seek: float = 0.0) -> None:
        if not self.playlist:
            print("[HazmatPlayer] Playlist is empty. Use: --add <file_or_url>")
            return
        if index < 0 or index >= len(self.playlist):
            print(f"[HazmatPlayer] Index out of range (0..{len(self.playlist) - 1}).")
            return

        with self._lock:
            was_running = self._proc is not None and self._proc.poll() is None
            if was_running:
                self._manual_jump = True
                self.current_index = index
                if self.is_paused:
                    self._suspend_resume_process(False)
                    self.is_paused = False
                self._proc.terminate()
                return

        t = threading.Thread(target=self._playback_worker, args=(index, seek), daemon=True)
        t.start()

    def next_track(self) -> None:
        if not self.playlist:
            print("[HazmatPlayer] Playlist is empty.")
            return
        next_idx = (self.current_index + 1) % len(self.playlist)
        self.play_index(next_idx)

    def previous_track(self) -> None:
        if not self.playlist:
            print("[HazmatPlayer] Playlist is empty.")
            return
        prev_idx = (self.current_index - 1 + len(self.playlist)) % len(self.playlist)
        self.play_index(prev_idx)

    # --- Other CLI Actions ---

    def action_snapshot(self, source: str, output: str = "snapshot.png", timestamp: float = 0.0) -> None:
        cmd = [
            self.ffmpeg, "-y",
            "-ss", str(max(0.0, timestamp)),
            "-i", source,
            "-frames:v", "1", "-q:v", "2",
            output,
        ]
        subprocess.run(cmd, check=True)
        print(f"[HazmatPlayer] Snapshot saved to {output}")

    def action_record(self, source: str, output: str = "recording.mkv", duration: Optional[float] = None) -> None:
        cmd = [self.ffmpeg, "-y", "-i", source]
        if duration and duration > 0:
            cmd += ["-t", str(duration)]
        cmd += ["-c", "copy", output]
        subprocess.run(cmd, check=True)
        print(f"[HazmatPlayer] Stream recorded to {output}")

    def action_convert(self, source: str, output: str) -> None:
        cmd = [self.ffmpeg, "-y"]
        if os.name == "nt":
            cmd += ["-hwaccel", "auto"]
        cmd += ["-i", source]
        af = self._build_audio_filters()
        vf = self._build_video_filters()
        if af:
            cmd += ["-af", ",".join(af)]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd.append(output)
        subprocess.run(cmd, check=True)
        print(f"[HazmatPlayer] Output exported to {output}")

    def action_info(self, source: str) -> None:
        category = self.detect_category(source)
        cmd = [self.ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", source]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        data["hazmatplayer_category"] = category
        data["hazmatplayer_version"] = self.VERSION
        print(json.dumps(data, indent=2))

    # --- Interactive Non-Blocking Shell (All commands start with --) ---

    def print_shell_help(self) -> None:
        print(
            "HazmatPlayer Interactive Commands (all commands start with --):\n"
            "  --add <src1> [src2 ...]        Add files or URLs to playlist\n"
            "  --playlist                     Show current playlist\n"
            "  --clear                        Clear playlist and stop playback\n"
            "  --play [index|source]          Start playback, jump to index, or add & play source\n"
            "  --pause                        Pause / Resume active playback\n"
            "  --stop                         Stop active playback\n"
            "  --next                         Skip to next item in playlist\n"
            "  --prev                         Go to previous item in playlist\n"
            "  --repeat <off|one|all>         Set playlist repeat mode\n"
            "  --muted <on|off>               Mute or unmute audio output\n"
            "  --volume <0-100>               Set base volume (applies on next track/restart)\n"
            "  --extra-volume <1.0-3.0>       Set extra gain boost factor\n"
            "  --eq <b1,b2,b3,b4,b5|off>      Set 5-band EQ gains in dB (60,230,910,3600,14000 Hz)\n"
            "  --rate <speed>                 Set playback speed rate (e.g. 1.25)\n"
            "  --link-pitch <on|off>          Link audio pitch to playback speed\n"
            "  --subtitle <file|off>          Attach local subtitle file\n"
            "  --snapshot <src> [out] [sec]   Capture PNG frame snapshot\n"
            "  --record <src> [out] [sec]     Record stream or media source\n"
            "  --convert <src> <out>          Convert/process media using system codecs\n"
            "  --info <src>                   Inspect media metadata (JSON)\n"
            "  --version                      Show HazmatPlayer version\n"
            "  --help                         Show this command list\n"
            "  --exit                         Quit HazmatPlayer"
        )

    def run_interactive_shell(self) -> None:
        print(f"=== HazmatPlayer Interactive Shell v{self.VERSION} ===")
        print("Type '--help' for commands. All commands must start with '--'.\n")

        while True:
            try:
                raw = input("hazmatplayer> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[HazmatPlayer] Exiting...")
                self.stop_current(user_initiated=True)
                break

            if not raw:
                continue

            try:
                tokens = shlex.split(raw, posix=(os.name != "nt"))
            except ValueError as e:
                print(f"[HazmatPlayer] Parse error: {e}")
                continue

            cmd = tokens[0].lower()
            args = tokens[1:]

            if not cmd.startswith("--"):
                print(f"[HazmatPlayer] Invalid syntax '{cmd}'. All commands must start with '--' (type '--help').")
                continue

            try:
                if cmd in ("--exit", "--quit"):
                    self.stop_current(user_initiated=True)
                    break
                elif cmd == "--help":
                    self.print_shell_help()
                elif cmd == "--version":
                    print(f"HazmatPlayer {self.VERSION}")
                elif cmd == "--add":
                    if not args:
                        print("Usage: --add <file_or_url> ...")
                    else:
                        self.add_to_playlist(args)
                elif cmd == "--playlist":
                    self.show_playlist()
                elif cmd == "--clear":
                    self.clear_playlist()
                elif cmd == "--play":
                    if args:
                        if args[0].isdigit():
                            self.play_index(int(args[0]))
                        else:
                            start_idx = len(self.playlist)
                            self.add_to_playlist(args)
                            self.play_index(start_idx)
                    else:
                        if self.is_paused:
                            self.toggle_pause()
                        else:
                            idx = self.current_index if self.current_index >= 0 else 0
                            self.play_index(idx)
                elif cmd == "--pause":
                    self.toggle_pause()
                elif cmd == "--stop":
                    self.stop_current(user_initiated=True)
                    print("[HazmatPlayer] Stopped.")
                elif cmd == "--next":
                    self.next_track()
                elif cmd == "--prev":
                    self.previous_track()
                elif cmd == "--repeat" and args:
                    if args[0] in ("off", "one", "all"):
                        self.repeat_mode = args[0]
                        print(f"[HazmatPlayer] Repeat mode: {self.repeat_mode}")
                    else:
                        print("Usage: --repeat <off|one|all>")
                elif cmd == "--muted" and args:
                    self.is_muted = args[0].lower() in ("on", "true", "1")
                    print(f"[HazmatPlayer] Muted: {self.is_muted}")
                elif cmd == "--volume" and args:
                    self.volume = max(0, min(100, int(args[0])))
                    print(f"[HazmatPlayer] Volume set to {self.volume}%")
                elif cmd == "--extra-volume" and args:
                    self.extra_volume = max(1.0, min(3.0, float(args[0])))
                    print(f"[HazmatPlayer] Extra volume set to {self.extra_volume}x")
                elif cmd == "--eq" and args:
                    if args[0].lower() == "off":
                        self.eq_gains = None
                        print("[HazmatPlayer] EQ disabled.")
                    else:
                        self.eq_gains = parse_eq(args[0])
                        print(f"[HazmatPlayer] EQ set to {self.eq_gains}")
                elif cmd == "--rate" and args:
                    self.rate = float(args[0])
                    print(f"[HazmatPlayer] Rate set to {self.rate}x")
                elif cmd == "--link-pitch" and args:
                    self.link_pitch = args[0].lower() in ("on", "true", "1")
                    print(f"[HazmatPlayer] Link pitch: {self.link_pitch}")
                elif cmd == "--subtitle" and args:
                    self.subtitle = None if args[0].lower() == "off" else args[0]
                    print(f"[HazmatPlayer] Subtitle: {self.subtitle}")
                elif cmd == "--snapshot" and args:
                    out = args[1] if len(args) > 1 else "snapshot.png"
                    sec = float(args[2]) if len(args) > 2 else 0.0
                    self.action_snapshot(args[0], out, sec)
                elif cmd == "--record" and args:
                    out = args[1] if len(args) > 1 else "recording.mkv"
                    dur = float(args[2]) if len(args) > 2 else None
                    self.action_record(args[0], out, dur)
                elif cmd == "--convert" and len(args) >= 2:
                    self.action_convert(args[0], args[1])
                elif cmd == "--info" and args:
                    self.action_info(args[0])
                else:
                    print(f"[HazmatPlayer] Unknown or incomplete command '{cmd}'. Type '--help'.")
            except Exception as e:
                print(f"[HazmatPlayer Error]: {e}")


def parse_eq(eq_str: Optional[str]) -> Optional[List[float]]:
    if not eq_str:
        return None
    parts = [float(x.strip()) for x in eq_str.split(",") if x.strip()]
    if len(parts) > 5:
        raise HazmatPlayerError("INVALID_EQ", "[HazmatPlayer] Equalizer accepts up to 5 bands (60, 230, 910, 3600, 14000 Hz).")
    return parts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hazmatplayer",
        description="HazmatPlayer CLI v6.6.0 MFULL - Non-Blocking Interactive Multimedia CLI",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("--version", action="version", version=f"HazmatPlayer {HazmatPlayerCLI.VERSION}")

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument("--play", metavar="SOURCE", help="Play source in background and open interactive '--' prompt")
    action_group.add_argument("--playlist", nargs="+", metavar="ITEM", help="Load playlist items and start interactive '--' prompt")
    action_group.add_argument("--shell", action="store_true", help="Open interactive HazmatPlayer shell directly")
    action_group.add_argument("--snapshot", metavar="SOURCE", help="Capture a PNG snapshot")
    action_group.add_argument("--record", metavar="SOURCE", help="Record a stream via stream copy")
    action_group.add_argument("--convert", metavar="SOURCE", help="Convert/process media using system codecs")
    action_group.add_argument("--info", metavar="SOURCE", help="Inspect stream metadata (JSON)")

    parser.add_argument("-o", "--output", default=None, help="Output file path")
    parser.add_argument("--repeat", choices=["off", "one", "all"], default="off", help="Playlist repeat mode")
    parser.add_argument("--muted", action="store_true", help="Mute audio output")
    parser.add_argument("--volume", type=int, default=100, help="Base volume 0-100")
    parser.add_argument("--extra-volume", type=float, default=1.0, help="Extra gain factor 1.0-3.0")
    parser.add_argument("--eq", type=str, default=None, help="5-band EQ gains in dB: 60,230,910,3600,14000")
    parser.add_argument("--rate", type=float, default=1.0, help="Playback speed rate")
    parser.add_argument("--link-pitch", action="store_true", help="Link pitch to speed")
    parser.add_argument("--subtitle", type=str, default=None, help="Subtitle file path")
    parser.add_argument("--seek", type=float, default=0.0, help="Seek timestamp in seconds")
    parser.add_argument("--duration", type=float, default=None, help="Recording duration limit")

    parser.add_argument("--ffmpeg-path", default=None)
    parser.add_argument("--ffplay-path", default=None)
    parser.add_argument("--ffprobe-path", default=None)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cli = HazmatPlayerCLI(
        ffmpeg_bin=args.ffmpeg_path,
        ffplay_bin=args.ffplay_path,
        ffprobe_bin=args.ffprobe_path,
    )
    cli.repeat_mode = args.repeat
    cli.is_muted = args.muted
    cli.volume = args.volume
    cli.extra_volume = args.extra_volume
    cli.eq_gains = parse_eq(args.eq)
    cli.rate = args.rate
    cli.link_pitch = args.link_pitch
    cli.subtitle = args.subtitle

    try:
        if args.play:
            cli.add_to_playlist([args.play])
            cli.play_index(0, seek=args.seek)
            cli.run_interactive_shell()
        elif args.playlist:
            cli.add_to_playlist(args.playlist)
            cli.play_index(0, seek=args.seek)
            cli.run_interactive_shell()
        elif args.shell or not any([args.snapshot, args.record, args.convert, args.info]):
            cli.run_interactive_shell()
        elif args.snapshot:
            cli.action_snapshot(args.snapshot, args.output or "snapshot.png", args.seek)
        elif args.record:
            cli.action_record(args.record, args.output or "recording.mkv", args.duration)
        elif args.convert:
            if not args.output:
                raise HazmatPlayerError("MISSING_OUTPUT", "[HazmatPlayer] --convert requires -o / --output <file>.")
            cli.action_convert(args.convert, args.output)
        elif args.info:
            cli.action_info(args.info)

        return 0
    except HazmatPlayerError as err:
        print(f"Error [{err.code}]: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
