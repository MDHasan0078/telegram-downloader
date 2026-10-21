"""Flet build entry point for the packaged GUI (APK / DEB).

The `flet build` tool expects `<module-name>.py` at the project root
(default `main.py`). This shim adds the `src/` layout to the path and
launches the real GUI from the package.

The Android fix has to happen HERE, before the package is imported, and
that ordering is the whole trick: `telegram_downloader.config` computes
CONFIG_DIR at IMPORT time, so anything that has to change where the
settings live has to run before this file imports the GUI.

Importing `telegram_downloader._android_paths` does not import
`config.py` -- the package's __init__ is only a version string -- which
is exactly what makes the fix possible.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from telegram_downloader import _android_paths  # noqa: E402

if _android_paths.is_android():
    _android_paths.point_config_at_writable_storage()

from telegram_downloader.gui import run_gui  # noqa: E402

run_gui()
