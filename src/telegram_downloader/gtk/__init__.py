"""The GTK4 GUI.

A native GTK4 front end for the same engine the CLI and the Flet build use.
The Linux .deb ships no GUI client: `flet` pulls `flet-desktop` only under
the `desktop` extra, so a .deb install reaches PyPI at runtime, pip-installs
flet-web + fastapi + starlette + uvicorn unpinned, and crashes. With GTK4
from apt the distro provides the widgets, there is nothing to download, and
the .deb stays hermetic.

The design system is a PORT, not a new one: `tokens.py` holds the same
palette, spacing and type scale as `telegram_downloader.ui`, and `theme.py`
builds the GTK4 stylesheet from them. Three rules ride on that palette and
all three survive the port:

1. ONE ACCENT, AND IT MEANS ACTION. Colour marks action or status, never
   "where am I": the selected destination is a RAISED surface with a
   hairline, the same material as a selected segment, never an accent fill.
2. THE TAB STRIP IS THE PAGE TITLE. The chips sit in the top bar and the
   content starts immediately below; there is no per-page heading.
3. EVERY LONG ACTION SHOWS IT IS WORKING. A button that awaits something
   swaps its label for a spinner and is disabled while it runs.

The engine is shared Python and is imported, never reimplemented: config,
downloader, converter, deps, updates, queue_store, tg_client.
"""
from __future__ import annotations

__all__ = ["run_gtk"]


def run_gtk() -> int:
    """Launch the GTK4 GUI. Returns the process exit code."""
    from .app import run_gtk as _run
    return _run()
