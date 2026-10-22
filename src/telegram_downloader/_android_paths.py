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
    """Whether this is Android, however the app got there.

    CPython exposes `getandroidapilevel` only on Android -- but only
    from 3.13, and this project runs 3.12, so that test alone would
    answer "not Android" on a 3.12 phone.

    The Android runtime sets ANDROID_PRIVATE in EVERY app's
    environment, whichever runtime the Python is embedded in. That
    matters here because there are two: Chaquopy (the native app) and
    serious_python (the Flet one). `platform.system()` says "Linux" on
    a phone exactly as it does on a desktop, so it cannot tell the two
    apart.

    ANDROID_PRIVATE and not ANDROID_ROOT: the latter is the legacy name
    for the Android SDK's directory, so it is set on a BUILD MACHINE
    too, and this would report a CI runner as a phone.
    """
    if hasattr(sys, "getandroidapilevel"):
        return True
    return bool(os.environ.get("ANDROID_PRIVATE"))


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

    The candidate is RESOLVED first, and that matters more than it looks.
    On Android the app's own storage is reached through
    /data/user/0/<package>/files, and /data/user/0 is a SYMLINK to
    /data/data. config.py walks every parent of the config dir and
    refuses the lot if any of them is a symlink, so the unresolved path
    is rejected with:

        RuntimeError: Refusing config dir traversing a symlink:
        /data/user/0/.../files/flet/app/src/xdg/telegram-downloader

    Resolving turns that into /data/data/<package>/files/..., which has
    no symlinked component, and it is the same directory on disk.

    Returns the directory it chose, or None if nothing was writable --
    in which case config.py's own (loud) refusal is left to speak.
    """
    for candidate in writable_dirs():
        try:
            # resolve() is what removes the /data/user/0 -> /data/data
            # symlink from the chain config.py checks.
            base = Path(candidate).resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
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
