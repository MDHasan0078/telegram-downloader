"""Android: put the app's settings somewhere it can actually write.

On Android the app's home directory is /data -- the passwd entry for the
app's uid -- and the app has no permission to write there. The config
layer refuses to run with an unwritable config dir, and it refuses
LOUDLY, which is how the published APK became a red error screen on
launch:

    PermissionError: [Errno 13] Permission denied: '/data/.config'
    RuntimeError: Refusing unsafe config dir
                 /data/.config/telegram-downloader

This module finds a directory the app CAN write to and points
XDG_CONFIG_HOME at it, which is the one knob config.py reads before it
computes CONFIG_DIR.

It lives in its own module, rather than in the APK's entry point, for
two reasons:

  - it is importable, so it can be tested;
  - importing it does NOT import config.py, which matters because
    config.py computes CONFIG_DIR at IMPORT time. The fix has to run
    before that import, and a module that cannot be imported without
    dragging config.py in cannot be used to do it.

Nothing here is Android-only in effect: the helpers are ordinary
functions, and the only Android-specific bit is the `sys` probe.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

__all__ = ["is_android", "point_config_at_writable_storage",
           "writable_dirs"]


def is_android() -> bool:
    """CPython exposes `getandroidapilevel` only on Android."""
    return hasattr(sys, "getandroidapilevel")


def writable_dirs() -> list[str]:
    """Places that might be writable for THIS app, best first.

    The app's own files directory is the one that matters, and Chaquopy
    extracts the app's Python into it, so it is on sys.path. Rather than
    trust any single one of these, every candidate is PROBED by the
    caller: a directory that cannot hold a file is not usable, whatever
    the environment says about it.
    """
    candidates: list[str] = []
    for var in ("ANDROID_PRIVATE", "XDG_DATA_HOME", "HOME"):
        value = os.environ.get(var)
        if value:
            candidates.append(value)
    candidates.extend(p for p in sys.path if p)
    try:
        candidates.append(tempfile.gettempdir())
    except Exception:
        pass
    return candidates


def _is_writable(directory: Path) -> bool:
    """Can this process actually create a file in *directory*?"""
    try:
        if not directory.is_dir():
            return False
        probe = directory / ".tgdl-write-probe"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def point_config_at_writable_storage() -> str | None:
    """Point XDG_CONFIG_HOME at a directory the app can write to.

    config.py validates the directory it is given: absolute, not a
    symlink, owned by the user, and not world- or group-writable. The
    directory is created 0700 here so those checks pass.

    Returns the directory it chose, or None if nothing was writable --
    in which case config.py's own (loud) refusal is left to speak.
    """
    for candidate in writable_dirs():
        try:
            base = Path(candidate)
        except (TypeError, ValueError):
            continue
        if not _is_writable(base):
            continue
        xdg = base / "xdg"
        try:
            xdg.mkdir(parents=True, exist_ok=True)
            os.chmod(xdg, 0o700)
        except OSError:
            continue
        os.environ["XDG_CONFIG_HOME"] = str(xdg)
        return str(xdg)
    return None
