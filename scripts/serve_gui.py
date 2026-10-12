"""Serve the Flet GUI over HTTP so it can be reached through a tunnel.

Codespaces has no display, so the desktop `ft.app()` cannot start. Flet also
ships a web client, so the same target runs as a normal HTTP server and any
browser can drive it.

    .venv/bin/python scripts/serve_gui.py [port]

This only changes HOW the app is presented. run_gui() and every view are
untouched, so what renders in the browser is the real GUI.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import flet as ft  # noqa: E402

from telegram_downloader.gui import run_gui  # noqa: E402


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8550
    # WEB_BROWSER serves the Flutter web client over HTTP. Without a view
    # flet tries to open a desktop window and dies on a headless box.
    run_gui(port=port, view=ft.AppView.WEB_BROWSER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())