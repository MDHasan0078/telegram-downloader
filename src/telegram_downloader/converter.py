"""FFmpeg helpers: detection, probing, MP4 conversion."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Tuple


def _sleep(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        threading.Event().wait(remaining)


def _bin(name: str) -> Optional[str]:
    """Resolve once to an absolute path so a hostile $PATH cannot swap
    the binary between our check and exec (all call sites use this)."""
    try:
        return shutil.which(name)
    except Exception:
        return None


def _reap(proc: subprocess.Popen) -> None:
    """Reap a dead child so no zombie / orphan ffmpeg is left behind.

    SIGTERM first (lets ffmpeg flush), then SIGKILL, waiting up to ~35 s
    total. proc.wait() can itself TimeoutExpired; never give up early and
    leak a zombie.
    """
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    for _ in range(30):
        try:
            proc.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            continue


def has_ffmpeg() -> bool:
    return _bin("ffmpeg") is not None


def has_ffprobe() -> bool:
    return _bin("ffprobe") is not None


def ffmpeg_version() -> Optional[str]:
    exe = _bin("ffmpeg")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
        return (r.stdout.splitlines() or [""])[0].strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def probe_codecs(input_path: Path) -> Tuple[Optional[str], Optional[str]]:
    exe = _bin("ffprobe")
    if not exe:
        return None, None
    try:
        v = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", "--", str(input_path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or None
        a = subprocess.run(
            [exe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", "--", str(input_path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or None
        return v, a
    except (OSError, subprocess.SubprocessError):
        return None, None


def ffprobe_has_video(path: Path) -> bool:
    exe = _bin("ffprobe")
    if not path.exists() or not exe:
        return False
    try:
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", "--", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return bool(r.stdout.strip()) and r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ffmpeg_to_mp4(input_path: Path, output_path: Path, mode: str = "2",
                  cancel: Optional[threading.Event] = None) -> None:
    """Create MP4 (remux when H.264/AAC-compatible, else re-encode).

    Writes only to *output_path*; callers should pass a `.part.mp4` temp
    name and atomically rename after success (see cli/gui).

    If *cancel* is supplied, ffmpeg runs under a Popen poll loop; setting
    the event kills the process (child-friendly SIGTERM, then SIGKILL) and
    raises RuntimeError("...cancelled...").
    """
    exe = _bin("ffmpeg")  # single resolve: same path checked and exec'd
    if not exe:
        raise RuntimeError("ffmpeg is required for MP4 output but was not found.")
    # Refuse to let ffmpeg -y truncate through a planted symlink/pipe.
    try:
        if output_path.is_symlink() or (output_path.exists() and not output_path.is_file()):
            raise RuntimeError(f"Refusing unsafe ffmpeg output path: {output_path}")
    except OSError as exc:
        raise RuntimeError(f"Cannot validate ffmpeg output path: {exc}") from exc
    video_codec, audio_codec = probe_codecs(input_path)
    can_remux = video_codec is not None and video_codec == "h264" and (audio_codec in {None, "aac"})
    if can_remux:
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(input_path), "-map", "0:v:0?", "-map", "0:a:0?",
               "-c", "copy", "-movflags", "+faststart", "--", str(output_path)]
    else:
        preset = "veryfast" if mode == "2" else "medium"
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(input_path), "-map", "0:v:0?", "-map", "0:a:0?",
               "-c:v", "libx264", "-preset", preset, "-crf", "28",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", "--", str(output_path)]
    try:
        if cancel is not None:
            proc = subprocess.Popen(cmd)
            while proc.poll() is None:
                if cancel.is_set():
                    # _reap terminates, then kills; never leaks a zombie or
                    # an orphaned ffmpeg that keeps holding the part file.
                    _reap(proc)
                    try:
                        output_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise RuntimeError(
                        f"Conversion cancelled; source kept at {input_path}")
                _sleep(0.2)
            if proc.returncode != 0:
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise RuntimeError(
                    f"FFmpeg processing failed; source kept at {input_path} "
                    f"(exit {proc.returncode})")
        else:
            subprocess.run(cmd, check=True, timeout=3600)
    except subprocess.TimeoutExpired as exc:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"FFmpeg timed out after 1h; source kept at {input_path}") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"FFmpeg processing failed; source kept at {input_path} ({exc})") from exc
    if not ffprobe_has_video(output_path):
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"FFmpeg reported success but the MP4 could not be verified: {output_path}\n"
            f"The source file was kept at: {input_path}"
        )
