"""FFmpeg helpers: detection, probing, MP4 conversion."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def has_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def ffmpeg_version() -> Optional[str]:
    if not has_ffmpeg():
        return None
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        return (r.stdout.splitlines() or [""])[0].strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def probe_codecs(input_path: Path) -> Tuple[Optional[str], Optional[str]]:
    if not has_ffprobe():
        return None, None
    try:
        v = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", "--", str(input_path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or None
        a = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", "--", str(input_path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or None
        return v, a
    except (OSError, subprocess.SubprocessError):
        return None, None


def ffprobe_has_video(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0 or not has_ffprobe():
        return False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", "--", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return bool(r.stdout.strip()) and r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ffmpeg_to_mp4(input_path: Path, output_path: Path, mode: str = "2") -> None:
    """Create MP4 (remux when H.264/AAC-compatible, else re-encode).

    Writes only to *output_path*; callers should pass a `.part.mp4` temp
    name and atomically rename after success (see cli/gui).
    """
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg is required for MP4 output but was not found.")
    video_codec, audio_codec = probe_codecs(input_path)
    can_remux = video_codec == "h264" and (audio_codec in {None, "aac"})
    if can_remux:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(input_path), "-map", "0:v:0?", "-map", "0:a:0?",
               "-c", "copy", "-movflags", "+faststart", "--", str(output_path)]
    else:
        preset = "veryfast" if mode == "2" else "medium"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(input_path), "-map", "0:v:0?", "-map", "0:a:0?",
               "-c:v", "libx264", "-preset", preset, "-crf", "28",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", "--", str(output_path)]
    try:
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
