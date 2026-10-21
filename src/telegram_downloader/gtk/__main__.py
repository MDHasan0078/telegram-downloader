"""`python -m telegram_downloader.gtk` -- launch the GTK4 GUI.

Kept as a module __main__ so the app can be started the same way as the
Flet one (`python -m telegram_downloader.gtk`).
"""
from __future__ import annotations

import sys

from . import run_gtk

if __name__ == "__main__":
    sys.exit(run_gtk())
