"""Dependency checks with per-OS install guidance.

If something is missing the app shows the exact command for the user's OS
(apt / dnf / pacman / brew / winget / pkg) instead of a generic error.
"""
from __future__ import annotations

import importlib.util
import platform
import re
import sys
from dataclasses import dataclass


def _strip_ansi(s: str) -> str:
    # A hostile/corrupt ffmpeg could echo ANSI escapes in its version line;
    # the dep status is rendered in GUI/CLI, keep it display-safe.
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x1f\x7f]", "", s).strip()


def _on_android() -> bool:
    """Whether this is Android, and so has no shell to install anything.

    `platform.system()` says "Linux" on a phone, and CPython's
    `getandroidapilevel` only exists from 3.13 -- this project runs
    3.12, and the Flet build embeds its Python through serious_python
    rather than Chaquopy. `_android_paths.is_android()` knows both
    cases: the Chaquopy one by its API, and the other by the
    ANDROID_PRIVATE the Android runtime sets in every app.
    """
    try:
        from ._android_paths import is_android
        return is_android()
    except Exception:
        return False


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
    required: bool = False
    # False for a dependency that does not apply to this platform AT ALL.
    #
    # A MISSING badge is a claim: "this is missing, and here is what to
    # do about it". On a phone there is nothing the user can do -- no
    # shell, no compiler, no package manager -- so a red MISSING badge
    # for ffmpeg or cryptg is an alarm about something they cannot fix
    # and the app does not even need. The row is left out of the list
    # instead, and the truth about what the app CAN do is told where
    # the user meets it: the output mode that needs ffmpeg is not
    # offered on Android, and says so.
    applicable: bool = True

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
    on_android = _on_android()

    # Python
    results.append(DepStatus(
        name="Python 3.10+",
        ok=sys.version_info >= (3, 10),
        version=platform.python_version(),
        hint="Upgrade Python to 3.10 or newer.",
        install_linux="sudo apt install python3 python3-venv  # Debian/Ubuntu",
        install_macos="brew install python@3.12",
        install_windows="winget install Python.Python.3.12",
        install_android="pkg install python",
    ))

    # ffmpeg (only needed for modes 2/3; mode 1 works without it)
    # Reuse converter's single _bin() resolution (avoids a TOCTOU double
    # resolve) and require REAL output: a present-but-broken binary
    # (non-zero exit) is reported as missing so the user fixes it instead of
    # the app silently treating an empty version as OK.
    from .converter import has_ffmpeg as _has_ffmpeg, ffmpeg_version as _ffmpeg_version
    raw_ver = _ffmpeg_version()
    ff_ok = _has_ffmpeg() and bool(raw_ver)
    if raw_ver:
        ff_ver = _strip_ansi(raw_ver)[:40]
    else:
        ff_ver = ""
    results.append(DepStatus(
        name="ffmpeg (for MP4 modes 2/3)",
        ok=ff_ok,
        version=ff_ver,
        hint=("Not available on Android: this app ships no ffmpeg and cannot "
              "install one. Original downloads (mode 1) work without it; the "
              "Fast and Max (MP4) modes cannot."
              if on_android else
              "Original downloads (mode 1) work without ffmpeg. Install it for compression."),
        install_linux="sudo apt install ffmpeg  # Debian/Ubuntu  |  sudo dnf install ffmpeg  |  sudo pacman -S ffmpeg",
        install_macos="brew install ffmpeg",
        install_windows="winget install Gyan.FFmpeg",
        install_android="pkg install ffmpeg",
        # On a phone the app does not offer the MP4 modes at all, so ffmpeg
        # is not a dependency of it. Listing it as MISSING would be an
        # alarm about something the user cannot install and the app does
        # not need.
        applicable=not on_android,
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
        required=True,
    ))

    # cryptg (optional speed-up)
    results.append(DepStatus(
        name="cryptg (optional, faster transfers)",
        ok=_has_module("cryptg"),
        version="",
        hint=("Not shipped on Android: cryptg is a compiled C extension. "
              "Telethon's pure-Python crypto is used instead -- slower, but "
              "it works."
              if on_android else
              "Recommended by Telethon for faster crypto; app works without it."),
        install_linux="pip install -U cryptg",
        install_macos="pip install -U cryptg",
        install_windows="pip install -U cryptg",
        install_android="pip install -U cryptg",
        # Same reasoning as ffmpeg: a phone has no compiler and no
        # package manager, so there is nothing the user can do about this
        # one, and the app works without it.
        applicable=not on_android,
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
    return [d for d in check_all() if d.required and not d.ok]
