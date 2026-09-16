"""Flet build entry point for the packaged GUI (APK / DMG / MSIX).

The `flet build` tool expects `<module-name>.py` at the project root
(default `main.py`). This shim adds the `src/` layout to the path and
launches the real GUI from the package.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from telegram_downloader.gui import run_gui  # noqa: E402

run_gui()