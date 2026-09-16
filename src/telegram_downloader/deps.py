"""Dependency checks with per-OS install guidance.

If something is missing the app shows the exact command for the user's OS
(apt / dnf / pacman / brew / winget / pkg) instead of a generic error.
"""
from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from dataclasses import dataclass


@dataclass
class DepStatus:
    name: str
    ok: bool
    version: str = ""
    hint: str = ""
    install_linux: str = ""
    install_macos: str = ""
    install_windows: str = ""
    install_android: str = ""

    def install_for_current_os(self) -> str:
        sysname = platform.system().lower()
        # Termux/Android reports Linux; detect via PREFIX env.
        import os
        if "com.termux" in os.environ.get("PREFIX", ""):
            return self.install_android or self.install_linux
        if sysname == "darwin":
            return self.install_macos
        if sysname == "windows":
            return self.install_windows
        return self.install_linux


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_all() -> list[DepStatus]:
    results: list[DepStatus] = []

    # Python
    results.append(DepStatus(
        name="Python 3.9+",
        ok=sys.version_info >= (3, 9),
        version=platform.python_version(),
        hint="Upgrade Python to 3.9 or newer.",
        install_linux="sudo apt install python3 python3-venv  # Debian/Ubuntu",
        install_macos="brew install python@3.12",
        install_windows="winget install Python.Python.3.12",
        install_android="pkg install python",
    ))

    # ffmpeg (only needed for modes 2/3; mode 1 works without it)
    ff_exe = shutil.which("ffmpeg")
    ff_ok = ff_exe is not None
    ff_ver = ""
    if ff_ok:
        try:
            import subprocess
            r = subprocess.run([ff_exe, "-version"], capture_output=True, text=True, timeout=10)
            ff_ver = (r.stdout.splitlines() or [""])[0].replace("ffmpeg version", "").strip()[:40]
        except Exception:
            pass
    results.append(DepStatus(
        name="ffmpeg (for MP4 modes 2/3)",
        ok=ff_ok,
        version=ff_ver,
        hint="Original downloads (mode 1) work without ffmpeg. Install it for compression.",
        install_linux="sudo apt install ffmpeg  # Debian/Ubuntu  |  sudo dnf install ffmpeg  |  sudo pacman -S ffmpeg",
        install_macos="brew install ffmpeg",
        install_windows="winget install Gyan.FFmpeg",
        install_android="pkg install ffmpeg",
    ))

    # Telethon (required)
    telethon_ver = ""
    if _has_module("telethon"):
        try:
            telethon_ver = __import__("telethon").__version__
        except Exception:
            telethon_ver = "(installed, version unknown)"
    results.append(DepStatus(
        name="telethon (required)",
        ok=_has_module("telethon"),
        version=telethon_ver,
        hint="Python Telegram client library.",
        install_linux="pip install -U telethon",
        install_macos="pip install -U telethon",
        install_windows="pip install -U telethon",
        install_android="pip install -U telethon",
    ))

    # cryptg (optional speed-up)
    results.append(DepStatus(
        name="cryptg (optional, faster transfers)",
        ok=_has_module("cryptg"),
        version="",
        hint="Recommended by Telethon for faster crypto; app works without it.",
        install_linux="pip install -U cryptg",
        install_macos="pip install -U cryptg",
        install_windows="pip install -U cryptg",
        install_android="pip install -U cryptg",
    ))

    # flet (GUI only)
    results.append(DepStatus(
        name="flet (GUI only)",
        ok=_has_module("flet"),
        version="",
        hint="Needed only for `tg-dl gui`. CLI works without it.",
        install_linux="pip install -U 'telegram-downloader[gui]'",
        install_macos="pip install -U 'telegram-downloader[gui]'",
        install_windows="pip install -U 'telegram-downloader[gui]'",
        install_android="pip install -U 'telegram-downloader[gui]'",
    ))
    return results


def missing_required() -> list[DepStatus]:
    return [d for d in check_all() if d.name.startswith("telethon") and not d.ok]
