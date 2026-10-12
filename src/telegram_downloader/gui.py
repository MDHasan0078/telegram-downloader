"""Flet desktop/mobile GUI — Material3 design matching simple-yt-downloader.

Achromatic body: structure comes from contrast and spacing, not hue. Colour is
reserved for STATUS (blue = active transfer, amber = converting, emerald =
done, red = failed) plus one Telegram-blue title bar. Header blocks (tinted
icon + bold title + subtitle), section cards (20px padding, icon + title +
optional subtitle), info rows, state-colored queue rows with % pills, progress
bars, byte details and error containers.

Chrome mirrors simple-yt-downloader: navigation is a tab strip across the TOP
(Add / Queue / Settings), the download folder is pinned to the BOTTOM and is
editable from any tab, and theme is toggled from the title bar. Dependencies
is a Settings section rather than its own page.

Run:  tg-dl gui   (needs `pip install -e '.[gui]'`)
"""
from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from . import converter, deps as deps_mod
from . import tg_client
from . import updates
from .config import (_sanitize_display, Settings, clear_session,
                     default_download_dir, ensure_download_dir,
                     harden_session_files, load_settings,
                     save_settings)
from .constants import MAX_BATCH_BYTES, MAX_URLS
from .downloader import Cancelled, download_resumable
from .queue_store import DownloadItem, QueueStore
from .telegram_utils import format_bytes, safe_filename, split_urls

import re as _re_module

# Neutral grey seed. The UI is deliberately achromatic: structure comes from
# contrast and spacing, not hue. Colour is reserved for STATUS below, so an
# active transfer, a success and a failure are the only saturated things on
# screen. This replaces an indigo seed that made every surface, icon, border
# and selected tab purple.
SEED = "#8A909E"  # neutral grey-400

# -- Title bar ---------------------------------------------------------------
# The one place colour is allowed outside the status palette. The body is
# deliberately achromatic, so the app bar is the single blue band that says
# "Telegram app" at a glance.
#
# Text is near-black, NOT white: #2AABEE with white text is 2.57:1, which fails
# WCAG AA (4.5:1) for body text outright. Dark ink on the same blue is 7.33:1,
# comfortably past AA. This is the same lightness lesson as the snackbars, where
# the rule already is: pick ink or paper per background, never by habit.
APPBAR_BG = "#2AABEE"  # Telegram brand blue
APPBAR_FG = "#0F1117"  # dark ink — see contrast note above

# -- Status colors (designed for dark-first palette, good contrast on dark bg) --
STATUS_COLORS = {
    "queued":      "#94A3B8",   # slate-400 — idle/neutral
    "fetching":    "#38BDF8",   # sky-400 — looking up metadata
    "downloading": "#60A5FA",   # blue-400 — active transfer
    "converting":  "#F59E0B",   # amber-500 — processing
    "done":        "#34D399",   # emerald-400 — success
    "error":       "#F87171",   # red-400 — failure
    "cancelled":   "#64748B",   # slate-500 — muted
}

# -- Snackbar colors --
SNACK_SUCCESS_BG = "#14532D"  # green-900
SNACK_SUCCESS_FG = "#BBF7D0"  # green-200
SNACK_ERROR_BG   = "#7F1D1D"  # red-900
SNACK_ERROR_FG   = "#FECACA"  # red-200
SNACK_INFO_BG    = "#22252E"  # neutral card, not violet-950
SNACK_INFO_FG    = "#C9CDD8"  # neutral-200

# -- Dark / light scaffold backgrounds --
DARK_BG       = "#0F1117"   # near-black, slight blue cast
DARK_SURFACE  = "#171A23"   # card surface
LIGHT_BG      = "#F7F7FA"   # neutral grey, not a lavender tint
LIGHT_SURFACE = "#FFFFFF"

# -- Error / hint box styling --
ERROR_BOX_BG     = "#3D1515"
ERROR_BOX_BORDER = "#FF5252"
HINT_BOX_BG      = "#1B1E27"
HINT_BOX_BORDER  = "#3A4050"

_queue_widgets = {}  # item_id -> {card, progress_bar, pct_pill, detail_txt, ...}
MODE_LABELS = {"1": "Original", "2": "Fast", "3": "Max"}


def _page_service(page, cls):
    """Return a page-level flet *Service*, created once per page.

    In Flet 0.86 `ft.FilePicker` (and friends) derive from `Service`, not from
    the visual `Control` tree. `Service.init()` self-registers under
    `Page._services`, so it must be mounted on the page -- putting it in
    `page.overlay` also tells the Flutter client to render it as a visual,
    which produces a full-height `Unknown control: FilePicker` error stripe and
    a picker that never opens. Caching per page avoids re-registering on every
    click.
    """
    cache = getattr(page, "_flet_services", None)
    if cache is None:
        cache = {}
        try:
            page._flet_services = cache
        except Exception:
            pass
    svc = cache.get(cls)
    if svc is None:
        svc = cls()
        cache[cls] = svc
        page.add(svc)
    return svc


def app_logo_src() -> str | None:
    """The packaged app logo as a data URI, or None if unavailable.

    assets/icon.svg ships inside the package (see package-data in
    pyproject.toml), so the title bar shows the same mark in every
    install -- editable, wheel, or the .deb's Python environment.
    A data URI is self-contained: it needs no assets_dir (which
    ft.app() does not accept here) and renders in both the desktop
    and web views. Returns None on any failure so the caller can
    fall back to a drawn mark instead of a broken image box.
    """
    import base64

    try:
        from importlib.resources import files

        svg = (files("telegram_downloader") / "assets" / "icon.svg").read_bytes()
    except Exception:
        return None
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def run_gui(port=None, view=None):
    import flet as ft

    settings = load_settings()
    store = QueueStore()
    state = {
        "previews": [],   # list[dict]: url,title,ext,size,ok,error,selected
        "fetching": False,
        "downloading": False,
        "queuing": False,
        "client": None,
        "cancel": None,   # asyncio.Event for current batch
        "mode": settings.output_mode or "1",
    }
    nav: dict = {}  # filled in _main: {"goto": fn} so views can switch tabs

    # ------------------------------------------------------------ helpers
    async def get_client(page: ft.Page):
        st = load_settings()
        if not st.api_id or not st.api_hash:
            raise RuntimeError("Enter API ID + hash in Settings first.")
        fingerprint = (str(st.api_id), str(st.api_hash))
        if state["client"] is None or state.get("client_fp") != fingerprint:
            # Credentials changed (or first use): drop the stale client so
            # one account's session is never driven with another's keys.
            old = state.get("client")
            if old is not None:
                try:
                    await old.disconnect()
                except Exception:
                    pass
                state["client"] = None
            state["client"] = tg_client.build_client(st.api_id_int(), str(st.api_hash))
            state["client_fp"] = fingerprint
            state.pop("me_id", None)
        c = state["client"]
        if not c.is_connected():
            await c.connect()
            harden_session_files()
        # Identity pinning (mirrors CLI _login_cli): a planted session for a
        # different account is refused instead of auto-trusted. Checked once
        # per process; the pin itself is tamper-detection, not a boundary.
        if state.get("me_id") is None and await c.is_user_authorized():
            try:
                me = await c.get_me()
                me_id = str(getattr(me, "id", "") or "")
                pinned = (st.user_id or "").strip() if getattr(st, "user_id", None) else ""
                if pinned and me_id and pinned != me_id:
                    try:
                        await c.log_out()
                    except Exception:
                        pass
                    try:
                        await c.disconnect()
                    except Exception:
                        pass
                    state["client"] = None
                    state.pop("client_fp", None)
                    clear_session()
                    # The pin is DELIBERATELY NOT cleared here. Clearing it
                    # made this guard one-shot: the first mismatch was
                    # refused, the user re-logged in, and the SECOND attempt
                    # re-pinned whatever session was present as trusted --
                    # so a planted session was adopted on the second try.
                    # That is exactly the attack this pin exists to stop, and
                    # it made the GUI strictly weaker than the CLI, which
                    # clears the session but keeps the pin. The way out is
                    # the explicit "this really is a different account"
                    # action in the login view (see _reset_identity_pin),
                    # never an automatic side effect of refusing.
                    raise RuntimeError(
                        "A different Telegram account is signed in than the one "
                        "this install is pinned to. For safety you have been "
                        "signed out. If that is expected, use 'Use a different "
                        "account' on the sign-in screen; otherwise this "
                        "session file was replaced by someone else.")
                if me_id:
                    state["me_id"] = me_id
            except RuntimeError:
                raise
            except Exception:
                pass
        return c

    _snackbars: dict = {}  # page id -> SnackBar (no page.snack_bar in Flet 0.86)

    def snack(page, msg: str, error: bool = False):
        try:
            sb = _snackbars.get(id(page))
            if sb is None:
                sb = ft.SnackBar(ft.Text(""))
                _snackbars[id(page)] = sb
                page.overlay.append(sb)
            sb.content = ft.Text(msg, color=SNACK_ERROR_FG if error else SNACK_SUCCESS_FG)
            sb.bgcolor = SNACK_ERROR_BG if error else SNACK_SUCCESS_BG
            sb.open = True
            page.update()
        except Exception:
            pass

    def app_version() -> str:
        # Never hardcode: read installed metadata, fall back to dev version.
        try:
            from importlib.metadata import version as _v
            return _v("telegram-downloader")
        except Exception:
            try:
                from . import __version__ as _dev
                return _dev
            except Exception:
                return "0.0.0-unknown"

    def apply_theme(page, theme: str) -> None:
        # Dark-first like the reference app: anything but "light" is dark.
        try:
            page.theme_mode = (ft.ThemeMode.LIGHT if (theme or "dark") == "light"
                               else ft.ThemeMode.DARK)
            page.update()
        except Exception:
            pass

    def install_cmd_for(dep) -> str:
        return dep.install_for_current_os()

    # ------------------------------------------------------------ design kit
    def h1(icon, title: str, subtitle: str | None = None):
        """Header block: tinted icon tile + bold title + optional subtitle."""
        title_col = [ft.Text(title, size=24, weight=ft.FontWeight.BOLD)]
        if subtitle:
            title_col.append(ft.Text(subtitle, size=13,
                                     color=ft.Colors.ON_SURFACE_VARIANT))
        return ft.Row([
            ft.Container(
                ft.Icon(icon, size=26, color=ft.Colors.ON_PRIMARY_CONTAINER),
                bgcolor=ft.Colors.PRIMARY_CONTAINER,
                border_radius=12, padding=8),
            ft.Column(title_col, spacing=2, expand=True),
        ], spacing=12)

    def section(title: str, body: list, icon=None, subtitle: str | None = None):
        """Section card: 20px padding, icon + bold title, optional subtitle."""
        head = [ft.Text(title, size=16, weight=ft.FontWeight.BOLD)]
        if subtitle:
            head.append(ft.Text(subtitle, size=12,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        if icon is not None:
            title_row = ft.Row([
                ft.Icon(icon, size=20, color=ft.Colors.PRIMARY),
                ft.Column(head, spacing=2, expand=True),
            ], spacing=8)
        else:
            title_row = ft.Column(head, spacing=2)
        return ft.Card(ft.Container(
            ft.Column([title_row, *body], spacing=12),
            padding=20,
            # Explicit surface + hairline border. The bare Card inherited the
            # default card colour, so sections read as one undifferentiated
            # slab with no separation from the scaffold behind them.
            bgcolor=ft.Colors.SURFACE,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=12))

    def info_row(icon, label: str, value: str, trailing=None):
        row = [ft.Icon(icon, size=20, color=ft.Colors.PRIMARY),
               ft.Column([ft.Text(label, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                          ft.Text(value, size=14)], spacing=2, expand=True)]
        if trailing is not None:
            row.append(trailing)
        return ft.Row(row, spacing=12)

    def info_row_ctrl(icon, label: str, value_ctrl, trailing=None):
        """Info row bound to a live control (for settings hints that refresh)."""
        row = [ft.Icon(icon, size=20, color=ft.Colors.PRIMARY),
               ft.Column([ft.Text(label, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                          value_ctrl], spacing=2, expand=True)]
        if trailing is not None:
            row.append(trailing)
        return ft.Row(row, spacing=12)

    def pct_pill(text: str):
        return ft.Container(
            ft.Text(text, size=11, weight=ft.FontWeight.BOLD,
                    color=ft.Colors.WHITE),
            bgcolor=ft.Colors.PRIMARY,
            border_radius=10, padding=6)

    def status_label(status: str):
        """Small theme-aware status chip for non-active queue rows."""
        # Use STATUS_COLORS for consistent, high-contrast colors
        fg = STATUS_COLORS.get(status, STATUS_COLORS["queued"])
        # Darker background tint for each status
        bg_map = {
            "queued": "#232733",
            "fetching": "#1E293B",
            "downloading": "#1E293B",
            "converting": "#4E342E",
            "done": "#1B5E20",
            "error": "#3D1515",
            "cancelled": "#263238",
        }
        bg = bg_map.get(status, "#2A2A3E")
        mapping = {
            "queued": (ft.Icons.SCHEDULE, "Queued — waiting to start"),
            "fetching": (ft.Icons.SYNC, "Fetching…"),
            "downloading": (ft.Icons.DOWNLOAD, "Downloading…"),
            "converting": (ft.Icons.SYNC, "Converting…"),
            "done": (ft.Icons.CHECK_CIRCLE, "Saved"),
            "error": (ft.Icons.ERROR, "Failed — see details below"),
            "cancelled": (ft.Icons.CANCEL, "Cancelled — partial kept for resume"),
        }
        icon, text = mapping.get(status, (ft.Icons.SCHEDULE, status))
        return ft.Container(
            ft.Row([ft.Icon(icon, size=14, color=fg),
                    ft.Text(text, size=11, weight=ft.FontWeight.W_500, color=fg)],
                   spacing=4, tight=True),
            bgcolor=bg, border_radius=8, padding=6)

    def error_box(message: str):
        _side = ft.BorderSide(1, ERROR_BOX_BORDER)
        return ft.Container(
            ft.Row([ft.Icon(ft.Icons.ERROR, size=16,
                            color=SNACK_ERROR_FG),
                    ft.Text(message, size=12, expand=True, selectable=True,
                            color=SNACK_ERROR_FG)],
                   spacing=8),
            bgcolor=ERROR_BOX_BG,
            border_radius=10, padding=12,
            border=ft.Border(top=_side, right=_side, bottom=_side, left=_side))

    def hint_box(message: str):
        """Neutral hint line: icon + wrapped secondary text with subtle border."""
        _side = ft.BorderSide(1, HINT_BOX_BORDER)
        return ft.Container(
            ft.Row([
                ft.Icon(ft.Icons.INFO_OUTLINE, size=16,
                        color=HINT_BOX_BORDER),
                ft.Text(message, size=12, expand=True,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=8),
            bgcolor=HINT_BOX_BG,
            border_radius=10, padding=10,
            border=ft.Border(top=_side, right=_side, bottom=_side, left=_side))

    def empty_state(icon, title: str, subtitle: str,
                    action_label: str | None = None,
                    action_icon=None, on_action=None):
        controls = [
            ft.Container(
                ft.Icon(icon, size=56, color=ft.Colors.PRIMARY),
                bgcolor=ft.Colors.PRIMARY_CONTAINER,
                border_radius=28, padding=20),
            ft.Text(title, size=22, weight=ft.FontWeight.BOLD),
            ft.Text(subtitle, size=14, color=ft.Colors.ON_SURFACE_VARIANT,
                    text_align=ft.TextAlign.CENTER),
        ]
        if action_label and on_action is not None:
            controls.append(ft.Container(
                ft.FilledButton(action_label, icon=action_icon,
                                on_click=on_action),
                margin=8))
        return ft.Container(
            ft.Column(controls, spacing=12,
                      alignment=ft.MainAxisAlignment.CENTER,
                      horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=48)

    def state_icon(status: str):
        # Use STATUS_COLORS for consistent, high-contrast colors
        fg = STATUS_COLORS.get(status, STATUS_COLORS["queued"])
        bg_map = {
            "queued": "#232733",
            "fetching": "#1E293B",
            "downloading": "#1E293B",
            "converting": "#4E342E",
            "done": "#1B5E20",
            "error": "#3D1515",
            "cancelled": "#263238",
        }
        bg = bg_map.get(status, "#2A2A3E")
        mapping = {
            "queued": ft.Icons.SCHEDULE,
            "fetching": ft.Icons.SYNC,
            "downloading": ft.Icons.DOWNLOAD,
            "converting": ft.Icons.SYNC,
            "done": ft.Icons.CHECK_CIRCLE,
            "error": ft.Icons.ERROR,
            "cancelled": ft.Icons.CANCEL,
        }
        icon = mapping.get(status, ft.Icons.SCHEDULE)
        return ft.Container(
            ft.Icon(icon, size=20, color=fg),
            bgcolor=bg, border_radius=14, padding=6)

    # ------------------------------------------------------------ ADD page
    def downloads_view(page: ft.Page):
        url_count_txt = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                                weight=ft.FontWeight.W_500)

        def _on_url_change(e):
            text = url_box.value or ""
            urls = _re_module.findall(r'https?://t\.me/\S+', text)
            n = len(urls)
            url_count_txt.value = f"{n} URL{'s' if n != 1 else ''} detected" if n else ""

        url_box = ft.TextField(
            label="Telegram link(s)",
            hint_text="https://t.me/channel/123\n(one per line)",
            prefix_icon=ft.Icons.LINK,
            multiline=True, min_lines=4, max_lines=8, expand=True,
            text_size=15, content_padding=14,
            border_radius=12,
            on_change=_on_url_change)
        preview_list = ft.ListView(spacing=12, expand=True)
        status_txt = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                             expand=True)
        count_txt = ft.Text("No previews yet",
                            size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                            weight=ft.FontWeight.W_500)
        selected_txt = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                               expand=True, weight=ft.FontWeight.W_500)
        fetch_btn = ft.FilledButton("Fetch titles", icon=ft.Icons.SEARCH,
                                    height=48, style=ft.ButtonStyle(
                                        text_style=ft.TextStyle(
                                            weight=ft.FontWeight.BOLD)))
        dl_btn = ft.FilledButton("Download selected", icon=ft.Icons.DOWNLOAD,
                                 disabled=True, height=48, style=ft.ButtonStyle(
                                     text_style=ft.TextStyle(
                                         weight=ft.FontWeight.BOLD)))
        fetch_label = ft.Text("Fetching titles...", size=13,
                              color=ft.Colors.PRIMARY,
                              weight=ft.FontWeight.W_600, visible=False)
        bar = ft.ProgressBar(visible=False, bar_height=6)
        fetch_row = ft.Row([fetch_label, bar], spacing=8, visible=False)

        # --- Mode selector: custom coloured cards ---
        _mode_data = [
            ("1", "Original", ft.Icons.FILE_DOWNLOAD,
             "Raw file, fastest, no conversion"),
            ("2", "Fast", ft.Icons.BOLT,
             "Compressed MP4, veryfast, smaller"),
            ("3", "Max", ft.Icons.HD,
             "Best quality compressed, smallest"),
        ]
        _mode_cards = {}
        # ResponsiveRow, not Row(wrap=True): a Row with wrap=True maps to
        # Flutter's Wrap, whose children carry FlexParentData from expand=True.
        # That combination collapsed the three cards into one full-width grey
        # block, making the primary control invisible and unclickable.
        _mode_row = ft.ResponsiveRow(spacing=12, run_spacing=12)

        def _refresh_mode_cards():
            current = state.get("mode", "1")
            for val, label, icon, desc in _mode_data:
                card = _mode_cards[val]
                is_sel = (val == current)
                card.content = ft.Column([
                    ft.Row([ft.Icon(icon, size=22,
                                    color=(ft.Colors.PRIMARY if is_sel
                                           else ft.Colors.ON_SURFACE_VARIANT)),
                            ft.Text(label, size=15,
                                    weight=(ft.FontWeight.BOLD if is_sel
                                            else ft.FontWeight.W_500),
                                    color=(ft.Colors.PRIMARY if is_sel
                                           else ft.Colors.ON_SURFACE))],
                           spacing=8),
                    ft.Text(desc, size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=4)
                card.border = (ft.Border.all(2, ft.Colors.PRIMARY)
                               if is_sel
                               else ft.Border.all(1, ft.Colors.OUTLINE_VARIANT))
                card.bgcolor = (ft.Colors.PRIMARY_CONTAINER if is_sel
                                else ft.Colors.SURFACE_CONTAINER_LOWEST)
                card.ink = True

        def _on_mode_card(val):
            state["mode"] = val
            mode_seg.selected = [val]
            _refresh_mode_cards()
            s = load_settings()
            s.output_mode = val
            save_settings(s)
            refresh_settings_hint()
            page.update()

        for val, label, icon, desc in _mode_data:
            c = ft.Container(
                ft.Column([], spacing=4),
                padding=14,
                border_radius=12,
                # Grid cell sizing instead of expand=True: three across on
                # desktop, full width on a phone.
                col={"xs": 12, "sm": 4},
                on_click=lambda e, v=val: _on_mode_card(v),
            )
            _mode_cards[val] = c
            _mode_row.controls.append(c)
        _refresh_mode_cards()

        # Hidden SegmentedButton kept for state compatibility
        mode_seg = ft.SegmentedButton(
            segments=[ft.Segment(value="1", label=ft.Text("Original"),
                                 icon=ft.Icons.FILE_DOWNLOAD,
                                 tooltip="Original: raw file, fastest"),
                      ft.Segment(value="2", label=ft.Text("Fast"),
                                 icon=ft.Icons.BOLT,
                                 tooltip="Fast: compressed MP4, veryfast"),
                      ft.Segment(value="3", label=ft.Text("Max"),
                                 icon=ft.Icons.HD,
                                 tooltip="Max: best quality compressed")],
            selected=[state["mode"] if state["mode"] in MODE_LABELS else "1"],
            visible=False)
        state["mode_seg"] = mode_seg

        def _on_mode(e):
            sel = e.control.selected
            if sel:
                state["mode"] = next(iter(sel))
                _refresh_mode_cards()
                s = load_settings()
                s.output_mode = state["mode"]
                save_settings(s)
                refresh_settings_hint()

        mode_seg.on_change = _on_mode

        async def ensure_api_or_goto_settings():
            st = load_settings()
            if not st.api_id or not st.api_hash:
                snack(page, "First add your Telegram API ID + hash in Settings.", error=True)
                if "goto" in nav:
                    nav["goto"](2)
                return None
            return st

        async def on_fetch(e):
            st = await ensure_api_or_goto_settings()
            if not st:
                return
            urls = split_urls(url_box.value or "")
            if not urls:
                snack(page, "Paste at least one t.me link first.", error=True)
                return
            if len(urls) > MAX_URLS:
                snack(page, f"Capped at {MAX_URLS} URLs; extra links ignored.")
            state["fetching"] = True
            fetch_row.visible = True
            status_txt.value = f"Fetching {len(urls)} title(s)..."
            fetch_btn.disabled = True
            page.update()
            try:
                c = await get_client(page)
                if not await c.is_user_authorized():
                    ok = await gui_login(page, c)
                    if not ok:
                        status_txt.value = "Login cancelled."
                        return
                # This block used to sit AFTER the `raise` at the end of the
                # RuntimeError handler below, which made it unreachable: the
                # try body ended at the `return` above, and every path out of
                # the handler either returned or re-raised. So "Fetch titles"
                # silently did nothing and dl_btn (born disabled) was never
                # enabled -- the GUI could not add a single download. It was
                # introduced by 8f91996, a security fix that inserted the
                # `raise` without moving the success path. It shipped in v1.3.0.
                state["previews"] = []
                for u in urls:
                    status_txt.value = f"Fetching: {_sanitize_display(u)}"
                    page.update()
                    pv = await tg_client.fetch_preview(c, u)
                    state["previews"].append({
                        "url": _sanitize_display(u),
                        "title": _sanitize_display(pv.title or u),
                        "ext": pv.ext,
                        "size": pv.size, "ok": pv.has_media and not pv.error,
                        "error": _sanitize_display(pv.error), "selected": pv.has_media and not pv.error,
                    })
                render_previews()
                n_ok = sum(1 for r in state["previews"] if r["ok"])
                status_txt.value = (f"Found {n_ok}/{len(urls)} downloadable. "
                                    f"Tick + edit titles, then Download selected.")
                dl_btn.disabled = n_ok == 0
            except RuntimeError as exc:
                # An identity-pin mismatch surfaces as a RuntimeError from
                # get_client(). Offer the explicit reset here rather than
                # leaving the user with a dead end: without this the pin is
                # never clearable from the GUI at all.
                msg = _sanitize_display(str(exc))
                if "pinned" in msg or "different Telegram account" in msg:
                    c = state.get("client")
                    if c is None:
                        from . import tg_client as _tc
                        try:
                            st0 = load_settings()
                            c = _tc.build_client(st0.api_id_int(), str(st0.api_hash))
                        except Exception:
                            c = None
                    if c is not None and await _reset_identity_pin(page, c):
                        status_txt.value = "Account pin cleared — sign in again."
                        snack(page, "Account pin cleared. Sign in with the "
                                    "account you want to use.")
                        page.update()
                        return
                    status_txt.value = msg
                    snack(page, msg, error=True)
                    page.update()
                    return
                raise
            except Exception as exc:
                status_txt.value = f"Fetch failed: {_sanitize_display(exc)}"
                snack(page, _sanitize_display(str(exc)), error=True)
            finally:
                state["fetching"] = False
                fetch_row.visible = False
                fetch_btn.disabled = False
                page.update()

        def render_previews():
            preview_list.controls.clear()
            oks = [r for r in state["previews"] if r["ok"]]
            bad = len(state["previews"]) - len(oks)
            if not state["previews"]:
                preview_list.controls.append(empty_state(
                    ft.Icons.DOWNLOAD_OUTLINED, "No previews yet",
                    "Paste links above and tap Fetch titles."))
                count_txt.value = "No previews yet"
            else:
                count_txt.value = (f"{len(oks)} downloadable"
                                   + (f"  \u2022  {bad} skipped" if bad else ""))
            n_sel = sum(1 for r in oks if r["selected"])
            selected_txt.value = (f"{n_sel} of {len(oks)} selected."
                                  if oks else "")
            dl_btn.disabled = n_sel == 0
            for i, r in enumerate(state["previews"]):
                if r["ok"]:
                    title_field = ft.TextField(
                        value=r["title"], label=f"Title {i + 1}",
                        dense=True, expand=True, border_radius=8,
                        content_padding=10,
                        on_change=lambda e, idx=i: state["previews"].__getitem__(idx).__setitem__("title", e.control.value))
                    chk = ft.Checkbox(
                        value=r["selected"],
                        tooltip="Include in download",
                        on_change=lambda e, idx=i: (
                            state["previews"].__getitem__(idx).__setitem__(
                                "selected", e.control.value),
                            render_previews()))
                    meta_row = ft.Row([
                        ft.Container(
                            ft.Row([ft.Icon(ft.Icons.DESCRIPTION, size=14,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                    ft.Text(r["ext"].upper(), size=11,
                                            weight=ft.FontWeight.W_600,
                                            color=ft.Colors.ON_SURFACE_VARIANT)],
                                   spacing=4, tight=True),
                            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                            border_radius=6, padding=4),
                        ft.Container(
                            ft.Row([ft.Icon(ft.Icons.STORAGE, size=14,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                    ft.Text(format_bytes(r["size"]), size=11,
                                            weight=ft.FontWeight.W_600,
                                            color=ft.Colors.ON_SURFACE_VARIANT)],
                                   spacing=4, tight=True),
                            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                            border_radius=6, padding=4),
                    ], spacing=8)
                    preview_list.controls.append(ft.Card(
                        elevation=2,
                        shape=ft.RoundedRectangleBorder(radius=12),
                        content=ft.Container(
                            ft.Row([
                                chk,
                                ft.Column([title_field, meta_row],
                                          spacing=6, expand=True),
                            ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.START),
                            padding=16)))
                else:
                    preview_list.controls.append(error_box(
                        f"{_sanitize_display(r['url'])}\nSkipped: {_sanitize_display(r['error'])}"))
            page.update()

        def on_select_all(e):
            for r in state["previews"]:
                if r["ok"]:
                    r["selected"] = True
            render_previews()

        def on_select_none(e):
            for r in state["previews"]:
                if r["ok"]:
                    r["selected"] = False
            render_previews()

        async def on_download(e):
            if state["queuing"]:
                return  # double-click / repeat click must not double-queue
            sel = [r for r in state["previews"] if r["ok"] and r["selected"]]
            if not sel:
                snack(page, "Nothing selected.", error=True)
                return
            st = load_settings()
            try:
                out_dir = ensure_download_dir(st)
            except (OSError, ValueError) as exc:
                snack(page, _sanitize_display(f"Cannot use download folder: {exc}"), error=True)
                return
            confirmed = await confirm_dialog(
                page, f"Download {len(sel)} file(s) to:\n{out_dir}?",
                "\n".join(f"• {safe_filename(r['title'])}{r['ext']}" for r in sel[:8])
                + (f"\n… +{len(sel) - 8} more" if len(sel) > 8 else ""))
            if not confirmed:
                return
            state["queuing"] = True
            dl_btn.disabled = True
            page.update()
            try:
                items = [DownloadItem(url=r["url"], title=safe_filename(r["title"]),
                                      ext=r["ext"], size=r["size"], status="queued",
                                      mode=r.get("mode") or state.get("mode") or st.output_mode or "1")
                         for r in sel]
                added = store.extend(items)
                if len(added) < len(items):
                    snack(page,
                          f"Queue near full: added {len(added)} of {len(items)} file(s).",
                          error=True)
                else:
                    snack(page, f"Queued {len(added)} file(s) — see Queue tab.")
            finally:
                state["queuing"] = False
            if "goto" in nav:
                nav["goto"](1)
            await start_queue(page)

        async def on_load_file(e):
            fp = _page_service(page, ft.FilePicker)
            try:
                res = await fp.pick_files(
                    dialog_title="Choose URL list",
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["txt"], allow_multiple=False)
            except Exception as exc:
                snack(page, f"Cannot open file picker: {exc}", error=True)
                return
            try:
                files = list(res or [])
                if files:
                    if files[0].path is None:
                        snack(page, "File picker returned no path.", error=True)
                        return
                    fpath = Path(files[0].path)
                    # No symlink/fifo bomb and no TOCTOU: open O_NOFOLLOW|O_RDONLY
                    # first, then fstat the fd (a swapped-in link raises ELOOP and
                    # the type/size checks run on the opened file itself).
                    import os as _os
                    import stat as _stat
                    import errno as _errno
                    nofollow = getattr(_os, "O_NOFOLLOW", 0)
                    try:
                        fd = _os.open(fpath, _os.O_RDONLY | nofollow)
                    except OSError as exc:
                        if exc.errno == _errno.ELOOP:
                            snack(page, "File must be a regular file (symlink refused).", error=True)
                            return
                        snack(page, f"Cannot open file: {exc}", error=True)
                        return
                    try:
                        st = _os.fstat(fd)
                        if not _stat.S_ISREG(st.st_mode):
                            snack(page, "File must be a regular file.", error=True)
                            return
                        if st.st_size > MAX_BATCH_BYTES:
                            snack(page, "File too large (max 256 KB). Split it or trim it.", error=True)
                            return
                        with _os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
                            fd = -1
                            txt = fh.read(257 * 1024)
                    finally:
                        if fd != -1:
                            try:
                                _os.close(fd)
                            except OSError:
                                pass
                    urls = split_urls(txt)
                    url_box.value = "\n".join(urls)
                    snack(page, f"Loaded {len(urls)} URL(s) from file.")
                page.update()
            except Exception as exc:
                snack(page, f"Cannot read file: {exc}", error=True)
            # No overlay teardown: the picker is mounted as a page service and
            # cached for the page's lifetime, so repeated picks are free and
            # nothing accumulates.

        def on_clear(e):
            url_box.value = ""
            state["previews"] = []
            preview_list.controls.clear()
            preview_list.controls.append(empty_state(
                ft.Icons.DOWNLOAD_OUTLINED, "No previews yet",
                "Paste links above and tap Fetch titles."))
            count_txt.value = "No previews yet"
            selected_txt.value = ""
            dl_btn.disabled = True
            status_txt.value = ""
            page.update()

        fetch_btn.on_click = on_fetch
        dl_btn.on_click = on_download
        render_previews()

        # --- Section header helper with icon badge ---
        def _section_header(icon, title, subtitle=None):
            head = [ft.Text(title, size=17, weight=ft.FontWeight.BOLD)]
            if subtitle:
                head.append(ft.Text(subtitle, size=12,
                                    color=ft.Colors.ON_SURFACE_VARIANT))
            return ft.Row([
                ft.Container(
                    ft.Icon(icon, size=18, color=ft.Colors.PRIMARY),
                    bgcolor=ft.Colors.PRIMARY_CONTAINER,
                    border_radius=8, padding=6),
                ft.Column(head, spacing=2, expand=True),
            ], spacing=10)

        return ft.Column([
            h1(ft.Icons.DOWNLOAD, "Telegram Downloader",
               "Paste t.me links, fetch titles, then queue downloads."),
            ft.Container(
                ft.Column([
                    _section_header(ft.Icons.LINK, "Add Download",
                                    "One link per line"),
                    ft.Container(height=8),
                    url_box,
                    ft.Row([url_count_txt], spacing=4),
                    ft.Container(height=8),
                    _section_header(ft.Icons.TUNE, "Output Mode",
                                    "Choose quality / speed trade-off"),
                    ft.Container(height=8),
                    _mode_row,
                    mode_seg,
                    ft.Container(height=12),
                    ft.Row([
                        fetch_btn,
                        ft.OutlinedButton("Load .txt",
                                          icon=ft.Icons.FILE_OPEN,
                                          on_click=on_load_file),
                        ft.TextButton("Clear", icon=ft.Icons.CLEAR,
                                      on_click=on_clear),
                    ], spacing=12, wrap=True),
                    fetch_row,
                    ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE, size=16,
                                    color=ft.Colors.ON_SURFACE_VARIANT),
                            status_txt], spacing=8),
                ], spacing=0),
                padding=20, border_radius=16,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            ),
            ft.Container(
                ft.Column([
                    _section_header(ft.Icons.PLAYLIST_ADD_CHECK,
                                    "Preview",
                                    "Confirm before download"),
                    ft.Container(height=8),
                    ft.Row([count_txt,
                            ft.TextButton("Select all", on_click=on_select_all),
                            ft.TextButton("Select none",
                                          on_click=on_select_none)],
                           spacing=8),
                    ft.Container(height=4),
                    preview_list,
                    ft.Container(height=8),
                    # No wrap=True: the Flutter Wrap cannot hold expand=True
                    # children, and the combination collapsed the layout into
                    # an unbounded (~100k px) scroll gap that pushed the primary
                    # "Download selected" CTA off-screen.
                    ft.Row([selected_txt, dl_btn],
                           spacing=12,
                           alignment=ft.MainAxisAlignment.END),
                ], spacing=0),
                padding=20, border_radius=16,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            ),
            ft.Container(
                ft.Column([
                    _section_header(ft.Icons.BOLT, "Quick Actions"),
                    ft.Container(height=8),
                    ft.Row([
                        ft.OutlinedButton("Open folder",
                                          icon=ft.Icons.FOLDER_OPEN,
                                          on_click=lambda e: open_folder(page)),
                        ft.OutlinedButton("Settings",
                                          icon=ft.Icons.SETTINGS,
                                          on_click=lambda e: nav["goto"](2) if "goto" in nav else None),
                    ], spacing=12, run_spacing=12, wrap=True),
                ], spacing=0),
                padding=20, border_radius=16,
                bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            ),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)

    def open_folder(page):
        # limit display of full path in snacks: redact home
        def _redact_home(p: Path) -> str:
            try:
                home = str(Path.home())
                ps = str(p)
                return "~" + ps[len(home):] if ps.startswith(home + "/") or ps == home else ps
            except Exception:
                return str(p)
        p = ensure_download_dir(load_settings())
        if not p.is_absolute():
            snack(page, f"Cannot use this folder: {_redact_home(p)}", error=True)
            return
        try:
            # No `--`: macOS open(1) and old xdg-open don't support bare
            # `--`, and is_absolute() already kills option-injection.
            # Resolved to absolute paths: hostile $PATH must not swap them.
            import shutil as _shutil
            if sys.platform.startswith("linux"):
                exe = _shutil.which("xdg-open")
                if not exe:
                    snack(page, "Cannot open folder: xdg-open not found.", error=True)
                    return
                subprocess.Popen([exe, str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen([_shutil.which("open") or "/usr/bin/open", str(p)])
            elif sys.platform == "win32":
                # explorer parses `,` `/select,` internally; reject odd chars
                # earlier in ensure_download_dir, keep list-form here.
                subprocess.Popen(["explorer", str(p)])
            else:
                snack(page, f"Folder: {p}")
        except Exception as exc:
            snack(page, f"{p} ({exc})")

    # ------------------------------------------------------------ queue runner
    async def start_queue(page: ft.Page):
        if state["downloading"]:
            return
        if not any(i.status == "queued" for i in store.items):
            snack(page, "Queue is empty (or all done). Retry an error or add new links.")
            return
        state["downloading"] = True
        state["cancel"] = asyncio.Event()
        refresh_queue()
        try:
            c = await get_client(page)
            st = load_settings()
            out_dir = ensure_download_dir(st)
            default_mode = st.output_mode or "1"
            # Re-drain: pick queued items one at a time rather than a snapshot,
            # so "Retry" or "Resume all" during a run is honoured instead of lost.
            while not state["cancel"].is_set():
                item = next((i for i in store.items if i.status == "queued"), None)
                if item is None:
                    break
                store.update(item.id, status="downloading", error="")
                mode = item.mode or default_mode

                def _cb(cur, tot, _id=item.id, _last=[0.0], _start=[0.0]):
                    if _start[0] == 0.0:
                        _start[0] = time.monotonic()
                    it = store.get(_id)
                    if it:
                        it.current_bytes = cur
                        it.total_bytes = tot
                        it.progress = (cur * 100 / tot) if tot else 0
                        elapsed = time.monotonic() - _start[0]
                        it.download_speed = (cur / elapsed) if elapsed > 0 else 0
                    # Throttle: redraw at most ~4x/s; the loop's trailing
                    # refresh_queue() covers the final state.
                    now = time.monotonic()
                    if now - _last[0] >= 0.25:
                        _last[0] = now
                        refresh_queue()
                try:
                    from .telegram_utils import parse_message_url
                    from .telegram_utils import infer_extension
                    try:
                        chat, mid, _ = parse_message_url(item.url)
                    except ValueError as exc:
                        store.update(item.id, status="error",
                                     error=f"Invalid message URL: {exc}")
                        refresh_queue()
                        continue
                    try:
                        # Race the metadata fetch against Cancel: without this a
                        # stuck get_messages holds the item "downloading" for up
                        # to 120 s with no way to stop it.
                        _mget = asyncio.ensure_future(c.get_messages(chat, ids=mid))
                        _cwait = asyncio.ensure_future(state["cancel"].wait())
                        done, _pending = await asyncio.wait(
                            {_mget, _cwait}, return_when=asyncio.FIRST_COMPLETED,
                            timeout=120)

                        async def _cancel_pending():
                            for t in (_mget, _cwait):
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(*_pending, return_exceptions=True)

                        if state["cancel"].is_set():
                            await _cancel_pending()
                            raise Cancelled(
                                "Cancelled while waiting for message metadata.")
                        if _mget in done:
                            msg = _mget.result()
                            if not _cwait.done():
                                _cwait.cancel()
                            await asyncio.gather(_cwait, return_exceptions=True)
                        else:
                            await _cancel_pending()
                            raise RuntimeError(
                                "Telegram didn't respond within 2 minutes. The service may be busy — please try again shortly.")
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError(
                            "Telegram didn't respond within 2 minutes. The service may be busy — please try again shortly.") from exc
                    if not msg or not getattr(msg, "media", None):
                        raise RuntimeError("Message has no downloadable media.")
                    stem = safe_filename(item.title or f"msg-{mid}")
                    ext = infer_extension(msg)
                    source = out_dir / f"{stem}{ext}"
                    target = out_dir / f"{stem}.mp4"
                    try:
                        out_res = out_dir.resolve()
                        if source.is_symlink() or target.is_symlink() or \
                                source.absolute().resolve().parent != out_res or \
                                target.absolute().resolve().parent != out_res:
                            raise RuntimeError("Refusing to write outside the download folder.")
                    except RuntimeError:
                        raise
                    except OSError:
                        pass
                    if mode in ("2", "3") and source.resolve() == target.resolve():
                        source = out_dir / f"{stem}.source{ext}"
                    await download_resumable(c, msg, source, progress_cb=_cb,
                                             cancel=state["cancel"])
                    if mode in ("2", "3"):
                        if not converter.has_ffmpeg():
                            store.update(item.id, status="done", progress=100.0, dest=str(source),
                                         error="ffmpeg missing — kept original.")
                            continue
                        store.update(item.id, status="converting")
                        refresh_queue()
                        import os as _os
                        import secrets as _secrets
                        import threading as _threading
                        tmp = target.with_name(
                            f"{target.stem}.part.{_os.getpid()}.{_secrets.token_hex(8)}.mp4")
                        nofollow = getattr(_os, "O_NOFOLLOW", 0)
                        try:
                            fd = _os.open(tmp, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | nofollow, 0o600)
                            _os.close(fd)
                            if tmp.is_symlink():
                                raise RuntimeError("Refusing to publish through symlink.")
                        except FileExistsError:
                            raise RuntimeError("Refusing unsafe converter temp file (exists).")
                        except OSError as exc:
                            import errno as _errno
                            if exc.errno == _errno.ELOOP:
                                raise RuntimeError("Refusing to publish through symlink.") from exc
                            raise
                        t_cancel = _threading.Event()

                        async def _watch_cancel():
                            await state["cancel"].wait()
                            t_cancel.set()

                        async def _deadline(seconds: int = 3600):
                            # A hung ffmpeg must not hold the item
                            # "converting" forever: hard-stop after an hour.
                            await asyncio.sleep(seconds)
                            t_cancel.set()
                        watch = asyncio.create_task(_watch_cancel())
                        clock = asyncio.create_task(_deadline())
                        try:
                            await asyncio.to_thread(
                                converter.ffmpeg_to_mp4, source, tmp, mode,
                                cancel=t_cancel)
                        finally:
                            watch.cancel()
                            clock.cancel()
                        if state["cancel"].is_set():
                            try:
                                tmp.unlink(missing_ok=True)
                            except OSError:
                                pass
                            store.update(item.id, status="cancelled",
                                         error="Cancelled by user.")
                            continue
                        if tmp.is_symlink():
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                            raise RuntimeError("Refusing to publish through symlink.")
                        if target.is_symlink():
                            try:
                                tmp.unlink(missing_ok=True)
                            except OSError:
                                pass
                            raise RuntimeError("Refusing to publish through symlink at target path.")
                        tmp.replace(target)
                        try:
                            if not source.is_symlink():
                                source.unlink(missing_ok=True)
                        except OSError:
                            pass
                        store.update(item.id, status="done", progress=100.0, dest=str(target))
                    else:
                        store.update(item.id, status="done", progress=100.0, dest=str(source))
                except Cancelled as exc:
                    store.update(item.id, status="cancelled", error=_sanitize_display(str(exc)))
                except Exception as exc:
                    if state["cancel"].is_set():
                        store.update(item.id, status="cancelled",
                                     error="Cancelled by user.")
                    else:
                        store.update(item.id, status="error", error=_sanitize_display(str(exc)))
                refresh_queue()
        except Exception as exc:
            snack(page, _sanitize_display(f"Download failed: {exc}"), error=True)
        finally:
            state["downloading"] = False
            refresh_queue()

    queue_list = None  # set by queue view; refreshed from anywhere
    queue_header_txt = {"ctrl": None, "summary": None}

    def refresh_queue():
        nonlocal _queue_empty_shown
        # Flet >= 0.86 raises RuntimeError reading .page of a detached control.
        try:
            pg = queue_list.page if queue_list is not None else None
        except RuntimeError:
            pg = None
        if pg is None:
            try:
                store.save()
            except Exception:
                pass
            return
        import flet as ft
        page = pg
        items = list(store.items)
        # Active + queued first, finished at the bottom (newest first within
        # each group, matching the old reversed chronological display).
        idx = {it.id: k for k, it in enumerate(store.items)}
        _rank = {"downloading": 0, "converting": 1, "fetching": 2,
                 "queued": 3, "done": 4, "cancelled": 5, "error": 6}
        items.sort(key=lambda i: (_rank.get(i.status, 7), -idx.get(i.id, 0)))
        n_total = len(items)
        n_active = sum(1 for i in items if i.status in ("downloading", "converting", "fetching"))
        n_queued = sum(1 for i in items if i.status == "queued")
        n_failed = sum(1 for i in items if i.status in ("error", "cancelled"))
        n_done = sum(1 for i in items if i.status == "done")
        if queue_header_txt["ctrl"] is not None:
            queue_header_txt["ctrl"].value = f"Downloads ({n_total})"
        if queue_header_txt["summary"] is not None:
            if not items:
                queue_header_txt["summary"].value = "Nothing here yet."
            else:
                ctrl = queue_header_txt["summary"]
                ctrl.value = ""
                # Only render counters that are non-zero. Rendering "0 need
                # attention" on a red chip put an alarm signal on a healthy
                # queue, and hardcoded WHITE on TERTIARY/ERROR measured
                # 1.9:1 / 2.8:1 against those light fills -- use the theme's
                # matching on_* roles instead.
                def _chip(n, label, bg, fg):
                    if n <= 0:
                        return []
                    return [ft.TextSpan(f" {n} {label} ",
                                       ft.TextStyle(color=fg, bgcolor=bg)),
                            ft.TextSpan("  ")]
                ctrl.spans = [
                    *_chip(n_active, "active", ft.Colors.PRIMARY,
                           ft.Colors.ON_PRIMARY),
                    *_chip(n_queued, "queued", ft.Colors.SURFACE_CONTAINER,
                           ft.Colors.ON_SURFACE_VARIANT),
                    *_chip(n_done, "done", ft.Colors.TERTIARY,
                           ft.Colors.ON_TERTIARY),
                    *_chip(n_failed, "need attention", ft.Colors.ERROR,
                           ft.Colors.ON_ERROR),
                ]
                if not ctrl.spans:
                    ctrl.value = "All clear."
        # Update the Queue tab badge with active + error count.
        badge_count = n_active + n_failed
        dest = queue_header_txt.get("tab")
        if dest is not None:
            try:
                dest.badge = (ft.Badge(str(badge_count),
                                       bgcolor=ft.Colors.ERROR)
                              if badge_count > 0 else None)
            except Exception:
                pass
        
        # Remove widgets for items no longer in queue
        current_ids = {it.id for it in items}
        for old_id in list(_queue_widgets.keys()):
            if old_id not in current_ids:
                del _queue_widgets[old_id]
        
        if not items:
            # An explicit flag, not a type/isinstance sniff: both the item
            # cards and empty_state() are ft.Container, so the old check could
            # never tell them apart and the empty state was never removed
            # (it also never called page.update() on this early return).
            if not _queue_empty_shown:
                queue_list.controls.clear()
                queue_list.controls.append(empty_state(
                    ft.Icons.DOWNLOAD_DONE, "No downloads in queue",
                    "Add links from the Add tab to get started.",
                    action_label="Add downloads", action_icon=ft.Icons.ADD,
                    on_action=lambda e: nav["goto"](0) if "goto" in nav else None))
                _queue_empty_shown = True
                page.update()
            return

        if _queue_empty_shown:
            queue_list.controls.clear()
            _queue_empty_shown = False
        
        # Build/update queue items incrementally
        new_controls = []
        for it in items:
            pct = max(0.0, min(100.0, it.progress or 0))
            
            # Check if we already have widgets for this item
            if it.id in _queue_widgets:
                # Update existing widgets
                widgets = _queue_widgets[it.id]
                # Update progress bar
                if widgets.get('progress_bar'):
                    widgets['progress_bar'].value = pct / 100
                # Update percentage pill
                if widgets.get('pct_pill'):
                    inner = widgets['pct_pill'].content
                    if inner is not None and hasattr(inner, "value"):
                        inner.value = f"{pct:.1f}%"
                # Update detail text
                if widgets.get('detail_txt'):
                    speed = getattr(it, "download_speed", 0) or 0
                    speed_txt = f"  \u2022  {format_bytes(speed)}/s" if speed > 0 else ""
                    detail = (f"{format_bytes(it.current_bytes)}/{format_bytes(it.total_bytes or it.size)}"
                              + speed_txt
                              + ("  \u2022  converting\u2026" if it.status == "converting"
                                 else "  \u2022  downloading\u2026"))
                    widgets['detail_txt'].value = detail
                # Add existing card to controls
                new_controls.append(widgets['card'])
            else:
                # Create new widgets for this item
                subtitle_bits = [b for b in
                                 [it.ext or "", format_bytes(it.total_bytes or it.size)] if b]
                actions = []
                if it.status in ("error", "cancelled"):
                    actions.append(ft.Container(
                        ft.IconButton(ft.Icons.REFRESH, tooltip="Retry",
                                      icon_color=ft.Colors.PRIMARY,
                                      style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))),
                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                        border_radius=8, padding=2))
                    actions[-1].content.on_click = lambda e, _id=it.id: (
                        store.update(_id, status="queued", error=""),
                        start_soon(page))
                if it.status in ("downloading", "converting", "fetching"):
                    actions.append(ft.Container(
                        ft.IconButton(ft.Icons.STOP_CIRCLE_OUTLINED,
                                      tooltip="Stop current download\n(rest stay queued)",
                                      icon_color=ft.Colors.ERROR,
                                      style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))),
                        bgcolor=ft.Colors.ERROR_CONTAINER,
                        border_radius=8, padding=2))
                    actions[-1].content.on_click = lambda e, _id=it.id: cancel_id(
                        page, _id)
                if it.status not in ("downloading", "converting", "fetching"):
                    actions.append(ft.Container(
                        ft.IconButton(ft.Icons.CLOSE, tooltip="Remove",
                                      icon_color=ft.Colors.ON_SURFACE_VARIANT,
                                      style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8))),
                        bgcolor=ft.Colors.SURFACE_CONTAINER,
                        border_radius=8, padding=2))
                    actions[-1].content.on_click = lambda e, _id=it.id: (
                        store.remove(_id), refresh_queue())
                title_txt = _sanitize_display(f"{it.title}{it.ext}", limit=120)
                title_col: list = [ft.Text(title_txt, size=14, weight=ft.FontWeight.W_600,
                                           max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)]
                if subtitle_bits:
                    title_col.append(ft.Text("  \u2022  ".join(subtitle_bits), size=11,
                                             color=ft.Colors.ON_SURFACE_VARIANT))
                
                # Create mutable widgets we'll update later
                pct_pill_widget = pct_pill(f"{pct:.1f}%") if it.status in (
                    "downloading", "converting") else ft.Container()
                
                head_row = ft.Row([state_icon(it.status),
                            ft.Column(title_col, spacing=2, expand=True),
                            pct_pill_widget,
                            *actions], spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER)
                card_body: list = [head_row]
                
                # Plain-language status line for idle states (queued/done/cancelled).
                if it.status in ("queued", "cancelled"):
                    card_body.append(status_label(it.status))
                
                progress_bar = None
                detail_txt = None
                if it.status in ("downloading", "converting"):
                    speed = getattr(it, "download_speed", 0) or 0
                    speed_txt = f"  \u2022  {format_bytes(speed)}/s" if speed > 0 else ""
                    detail = (f"{format_bytes(it.current_bytes)}/{format_bytes(it.total_bytes or it.size)}"
                              + speed_txt
                              + ("  \u2022  converting\u2026" if it.status == "converting"
                                 else "  \u2022  downloading\u2026"))
                    progress_bar = ft.ProgressBar(value=pct / 100, bar_height=8,
                                                  border_radius=4,
                                                  color=ft.Colors.PRIMARY,
                                                  bgcolor=ft.Colors.SURFACE_CONTAINER)
                    detail_txt = ft.Text(detail, size=11,
                                         color=ft.Colors.ON_SURFACE_VARIANT,
                                         weight=ft.FontWeight.W_500)
                    card_body += [progress_bar, detail_txt]
                
                if it.status == "error" and it.error:
                    card_body.append(error_box(_sanitize_display(it.error)))
                if it.status == "done":
                    if it.dest:
                        card_body.append(ft.Container(
                            ft.Row([
                                ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=16,
                                        color=ft.Colors.TERTIARY),
                                ft.Text(it.dest, size=11, expand=True,
                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ], spacing=6),
                            bgcolor=ft.Colors.TERTIARY_CONTAINER,
                            border_radius=8, padding=8))
                    elif it.error:
                        # Kept-original note (e.g. ffmpeg missing) surfaces here.
                        card_body.append(hint_box(it.error))
                
                card_side = ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)
                card = ft.Container(
                    ft.Column(card_body, spacing=8),
                    padding=14,
                    border_radius=12,
                    bgcolor=ft.Colors.SURFACE,
                    border=ft.Border(top=card_side, right=card_side,
                                     bottom=card_side, left=card_side))
                new_controls.append(card)
                
                # Store references to mutable widgets
                _queue_widgets[it.id] = {
                    'card': card,
                    'progress_bar': progress_bar,
                    # Only cache a REAL pill. The old guard was
                    # `hasattr(pct_pill_widget, "content")`, which is True for
                    # every ft.Container because `content` is a dataclass field
                    # -- a bare ft.Container() has content=None, so the next
                    # refresh did `None.value = ...` and raised AttributeError,
                    # killing the queue view (and with it the app).
                    'pct_pill': (pct_pill_widget
                                 if getattr(pct_pill_widget, "content", None) is not None
                                 else None),
                    'detail_txt': detail_txt,
                }
        
        queue_list.controls = new_controls
        try:
            page.update()
        except Exception:
            pass

    def start_soon(page):
        page.run_task(start_queue, page)

    def cancel_id(page, target_id):
        it = store.get(target_id)
        if not it:
            return
        if it.status == "queued":
            store.update(target_id, status="cancelled", error="Cancelled by user.")
            refresh_queue()
            snack(page, "Removed queued download.")
            return
        if it.status in ("downloading", "converting", "fetching"):
            # Only one batch can run at a time, so stopping the active item
            # naturally pauses the run. Be honest about it: the current file
            # is cancelled and the rest remain queued for "Resume all / Start".
            if state.get("cancel") and not state["cancel"].is_set():
                state["cancel"].set()
                snack(page, "Stopping this download — others stay queued (Resume all to continue).")
            else:
                snack(page, "Already stopping…", error=True)

    _queue_empty_shown = False

    def queue_view(page: ft.Page):
        import flet as ft
        nonlocal queue_list, _queue_empty_shown
        _queue_empty_shown = False
        queue_list = ft.ListView(spacing=10, expand=True)
        header = ft.Text(f"Downloads ({len(store.items)})", size=20,
                         weight=ft.FontWeight.BOLD)
        summary = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        queue_header_txt["ctrl"] = header
        queue_header_txt["summary"] = summary

        async def on_resume_all(e):
            store.reset_errors_to_queued()
            refresh_queue()
            await start_queue(page)

        def on_cancel(e):
            if state.get("cancel"):
                state["cancel"].set()
                snack(page, "Stopping queue — current file is cancelled, the rest stay queued.")

        toolbar = ft.Container(
            ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.DOWNLOADING, size=22,
                            color=ft.Colors.PRIMARY),
                    header,
                    ft.Container(expand=True),
                    ft.TextButton("Clear finished", icon=ft.Icons.DELETE_SWEEP,
                                  on_click=lambda e: (store.clear_finished(),
                                                      refresh_queue())),
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                summary,
                ft.Row([
                    ft.FilledButton("Resume all / Start",
                                    icon=ft.Icons.PLAY_ARROW,
                                    on_click=on_resume_all),
                    ft.OutlinedButton("Cancel batch", icon=ft.Icons.STOP_CIRCLE_OUTLINED,
                                      on_click=on_cancel),
                ], spacing=10, wrap=True),
            ], spacing=12),
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border_radius=14, padding=18)
        refresh_queue()
        return ft.Column([
            h1(ft.Icons.QUEUE, "Queue",
               "Track progress, retry errors, clear finished downloads."),
            toolbar,
            queue_list,
        ], spacing=16, expand=True)

    # ------------------------------------------------------------ settings
    settings_refs: dict = {}

    def refresh_settings_hint():
        try:
            st = load_settings()
            if settings_refs.get("mode_txt") is not None:
                settings_refs["mode_txt"].value = MODE_LABELS.get(st.output_mode, st.output_mode)
            if settings_refs.get("folder_txt") is not None:
                settings_refs["folder_txt"].value = st.download_dir or str(default_download_dir())
        except Exception:
            pass

    def settings_view(page: ft.Page):
        import flet as ft
        st = load_settings()
        # No prefix_icon: the field icons duplicated the section icon sitting
        # directly above them (folder/gear/gear), and M3 prefixed fields clip
        # the floating label into the border. The section header already
        # carries the icon, so the field only needs the label.
        _fld_border = ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)
        # TextField has no `focused_border`; the real fields are
        # focused_border_width / focused_border_color.
        _fld_kw = dict(border=_fld_border,
                       focused_border_width=2, focused_border_color=ft.Colors.PRIMARY,
                       border_radius=10, content_padding=ft.Padding(16, 14, 16, 14),
                       text_size=14)
        # Built per-view, not at import: ft.Colors.* resolves against the
        # active theme at access time, so a module-level constant would freeze
        # whichever theme happened to be active on first import.
        _label_style = ft.TextStyle(size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        api_id_f = ft.TextField(label="API ID", label_style=_label_style,
                                value=st.api_id or "", **_fld_kw)
        api_hash_f = ft.TextField(label="API hash (keep private)", label_style=_label_style,
                                  value=st.api_hash or "",
                                  password=True, can_reveal_password=True, **_fld_kw)
        dl_f = ft.TextField(label="Download folder", label_style=_label_style,
                            value=st.download_dir or str(default_download_dir()),
                            expand=True, **_fld_kw)
        folder_error = ft.Container(visible=False)

        def _validate_folder(e):
            path = (dl_f.value or "").strip()
            if not path:
                folder_error.visible = False
                try:
                    page.update()
                except Exception:
                    pass
                return
            try:
                tmp = Settings()
                tmp.download_dir = path
                ensure_download_dir(tmp)
                folder_error.visible = False
            except (ValueError, OSError) as exc:
                folder_error.content = error_box(f"Cannot use folder: {_sanitize_display(str(exc))}")
                folder_error.visible = True
            try:
                page.update()
            except Exception:
                pass

        dl_f.on_change = _validate_folder
        mode_txt = ft.Text(MODE_LABELS.get(st.output_mode, st.output_mode), size=14)
        folder_txt = ft.Text(st.download_dir or str(default_download_dir()), size=14,
                             max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        settings_refs.update(mode_txt=mode_txt, folder_txt=folder_txt)
        settings_error = ft.Container(visible=False)

        async def pick_dir(e):
            fp = _page_service(page, ft.FilePicker)
            try:
                res = await fp.get_directory_path(dialog_title="Choose download folder")
            except Exception as exc:
                snack(page, f"Cannot open folder picker: {exc}", error=True)
                return
            try:
                path = getattr(res, "path", res)  # event or plain string
                if path:
                    dl_f.value = path
                    page.update()
            except Exception as exc:
                snack(page, f"Cannot use folder: {exc}", error=True)

        async def on_save(e):
            s = load_settings()
            s.api_id = api_id_f.value.strip() or None
            s.api_hash = api_hash_f.value.strip() or None
            # download_dir is deliberately NOT written here. It is edited from
            # the always-visible bottom bar, which validates through
            # ensure_download_dir() and saves immediately. Assigning it from
            # this screen's (now removed) text field meant that changing the
            # folder at the bottom and then pressing Save in Settings silently
            # reverted it to whatever the stale field still held.
            if s.api_id and not s.api_id.isdigit():
                settings_error.content = error_box("API ID must be numeric.")
                settings_error.visible = True
                page.update()
                return
            if s.download_dir:
                try:
                    # Same containment as CLI: refuses config-dir overlap,
                    # root, and symlinks (raises ValueError).
                    ensure_download_dir(s)
                except (ValueError, OSError) as exc:
                    settings_error.content = error_box(f"Cannot use folder: {_sanitize_display(str(exc))}")
                    settings_error.visible = True
                    page.update()
                    return
            save_settings(s)
            refresh_settings_hint()
            settings_error.visible = False
            folder_error.visible = False
            page.update()
            snack(page, "Settings saved.")

        async def on_login(e):
            s = load_settings()
            if not api_id_f.value.strip() or not api_hash_f.value.strip():
                snack(page, "Save API ID + hash first.", error=True)
                return
            s.api_id, s.api_hash = api_id_f.value.strip(), api_hash_f.value.strip()
            save_settings(s)
            try:
                c = await get_client(page)
                if await c.is_user_authorized():
                    me = await c.get_me()
                    name = getattr(me, 'first_name', None) or getattr(me, 'username', None)
                    snack(page, f"Already logged in as {_sanitize_display(str(name))}.")
                    return
                ok = await gui_login(page, c)
                snack(page, "Login OK." if ok else "Login cancelled.")
            except Exception as exc:
                snack(page, f"Login failed: {exc}", error=True)

        async def on_logout(e):
            server_ok = False
            try:
                if state["client"]:
                    try:
                        await state["client"].log_out()
                        server_ok = True
                    except Exception:
                        server_ok = False
                    try:
                        await state["client"].disconnect()
                    except Exception:
                        pass
                    state["client"] = None
                    state.pop("client_fp", None)
                    state.pop("me_id", None)
                else:
                    # No live client: try a one-shot server logout so
                    # "logout" actually invalidates the server session.
                    try:
                        st0 = load_settings()
                        if st0.api_id and st0.api_hash:
                            tmp = tg_client.build_client(st0.api_id_int(), str(st0.api_hash))
                            try:
                                await tmp.connect()
                                try:
                                    await tmp.log_out()
                                    server_ok = True
                                except Exception:
                                    server_ok = False
                            finally:
                                try:
                                    await tmp.disconnect()
                                except Exception:
                                    pass
                    except Exception:
                        server_ok = False
            finally:
                try:
                    st0 = load_settings()
                    if getattr(st0, "user_id", None):
                        st0.user_id = None
                        save_settings(st0)
                except Exception:
                    pass
                clear_session()
                try:
                    from .config import CONFIG_DIR as _CD
                    leftover = any(_CD.glob("session*"))
                except OSError:
                    leftover = False
                if leftover:
                    snack(page, "Warning: session files remain (locked?). "
                          + ("Server logout ok." if server_ok else
                             "Server logout FAILED — revoke at my.telegram.org."),
                          error=True)
                elif not server_ok:
                    snack(page, "Local session removed, but server logout FAILED "
                          "(offline?). Revoke at my.telegram.org if shared.", error=True)
                else:
                    snack(page, "Logged out — server session revoked, files removed.")

        check_updates_btn = ft.FilledTonalButton("Check for Updates", icon=ft.Icons.UPDATE)

        async def on_check_updates(e):
            check_updates_btn.disabled = True
            check_updates_btn.text = "Checking..."
            check_updates_btn.icon = ft.Icons.HOURGLASS_EMPTY
            page.update()
            try:
                snack(page, "Checking for updates...")
                info = await asyncio.to_thread(updates.check_for_update)
                if info is None:
                    snack(page, "Could not reach the update server.", error=True)
                    return
                if not info.is_newer_than(app_version()):
                    snack(page, f"You are up to date (v{app_version()}).")
                    return
                choice = await update_dialog(page, info)
                if choice == "page":
                    try:
                        updates.open_release_page(info.release_url)
                    except Exception as exc:
                        snack(page, f"Cannot open browser: {exc}", error=True)
                elif choice == "download":
                    await download_installer(page, info)
            finally:
                check_updates_btn.disabled = False
                check_updates_btn.text = "Check for Updates"
                check_updates_btn.icon = ft.Icons.UPDATE
                page.update()

        # Bound here (not in the constructor): on_check_updates closes over
        # check_updates_btn, and the def must exist before it is referenced.
        check_updates_btn.on_click = on_check_updates

        async def download_installer(page, info):
            asset = updates.installable_asset(info)
            if asset is None:
                # .dmg needs a mounted image and a drag-to-Applications step;
                # .apk is an Android package a desktop cannot install. Send
                # the user to the release page instead of pretending.
                snack(page, "No installer for your OS — opening release page instead.", error=True)
                try:
                    updates.open_release_page(info.release_url)
                except Exception:
                    pass
                return
            # The check-for-updates button is the only control this flow can
            # be re-entered from; disable it so a second click cannot start a
            # second download/install while one is already in flight.
            check_updates_btn.disabled = True
            snack(page, f"Downloading {asset.name}...")
            try:
                token = os.environ.get("GITHUB_TOKEN") or ""
                dest = await asyncio.to_thread(
                    updates.download_asset, asset,
                    ensure_download_dir(load_settings()), info.latest_version,
                    token or None)
            except Exception as exc:
                snack(page, f"Download failed: {exc}", error=True)
                return
            finally:
                check_updates_btn.disabled = False

            # From here the file is verified and on disk. Installing runs it
            # as root through pkexec, so it is a separate, explicit step with
            # its own confirmation — never fused silently into the download.
            go = await confirm_dialog(
                page,
                "Install this update?",
                f"Downloaded and verified:\n{_sanitize_display(os.path.basename(str(dest)))}\n\n"
                "Installing runs apt-get with administrator rights. Your "
                "normal password prompt will appear. The app should be closed "
                "while it updates.",
                action="Install and restart", icon=ft.Icons.INSTALL_MOBILE)
            if not go:
                snack(page, f"Installer saved to {dest}.")
                return
            snack(page, "Installing… approve the password prompt to continue.")
            ok, msg = await asyncio.to_thread(updates.install_deb, dest)
            if ok:
                snack(page, msg)
                try:
                    page.window_close()
                except Exception:
                    pass
            else:
                snack(page, f"Install failed: {msg}", error=True)
                snack(page, f"The installer is still at {dest}.")

        def _about_rows():
            try:
                import telethon
                tver = telethon.__version__
            except Exception:
                tver = "missing"
            ff = converter.ffmpeg_version() or "Not detected"
            return [
                info_row(ft.Icons.API, "Version", f"v{app_version()}"),
                info_row(ft.Icons.STORAGE, "Engine", f"Telethon {tver}"),
                info_row(ft.Icons.VIDEO_FILE, "FFmpeg", ff),
            ]

        async def on_reset(e):
            # Destructive two-step: reset options, clear the saved session and
            # the pending download queue. A stray click must not nuke secrets.
            # The confirm button is labelled for what it actually does (Reset),
            # not the downloads dialog's default "Download".
            cp = await confirm_dialog(
                page,
                "Reset everything?",
                "This clears your saved credentials, logs out the Telegram "
                "session, and empties the download queue. This cannot be undone.",
                action="Reset everything", icon=ft.Icons.WARNING_AMBER)
            if not cp:
                return
            if state.get("cancel") and not state.get("cancel").is_set():
                state["cancel"].set()
            try:
                clear_session()
            except Exception:
                pass
            store.clear_all()
            save_settings(Settings())
            state["previews"] = []
            state["mode"] = "1"
            try:
                settings_refs["mode_txt"].value = "Original"
                settings_refs["folder_txt"].value = str(default_download_dir())
            except Exception:
                pass
            try:
                mg = state.get("mode_seg")
                if mg is not None:
                    mg.selected = ["1"]
            except Exception:
                pass
            refresh_queue()
            refresh_settings_hint()
            snack(page, "Reset complete. Re-enter API credentials.")

        # ── Redesigned layout ──────────────────────────────────────
        first = not st.api_id or not st.api_hash or not st.download_dir

        # --- Section 1: API Credentials ---
        api_card_body = []
        if first:
            api_card_body.append(hint_box(
                "API credentials are required once, then stored privately (0600)."))
        api_card_body += [
            api_id_f,
            api_hash_f,
        ]
        # Cards use the shared section() helper rather than hand-rolling the same
        # icon+title+subtitle header four times. It also means they pick up the
        # hairline border and explicit surface, which the elevation-only cards
        # here were missing -- they read as one flat slab against the scaffold.
        api_card = section("API Credentials", api_card_body,
                           icon=ft.Icons.KEY,
                           subtitle="Asked once, stored privately (0600).")

        # Download folder moved out of Settings: it lives in the bottom bar
        # and is editable from any tab. Keeping a second copy here meant two
        # controls for one value, and pressing Save below would overwrite the
        # bottom bar's change with whatever this stale field held.

        # --- Section 2: Output Mode ---
        mode_card = section(
            "Output Mode",
            [
                ft.Container(
                    ft.Row([
                        ft.Icon(ft.Icons.PLAY_CIRCLE, size=22,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        ft.Text("Default: ", size=13,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        mode_txt,
                    ], spacing=8),
                    padding=12,
                    border_radius=8,
                    border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                ),
                ft.Text("Change it on the Add tab.", size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            icon=ft.Icons.TUNE,
            subtitle="Default mode for new downloads.")

        # --- Section 3: Actions ---
        action_buttons = ft.Row([
            ft.FilledButton("Save Settings", icon=ft.Icons.SAVE,
                            on_click=on_save),
            ft.FilledTonalButton("Login / Verify", icon=ft.Icons.LOGIN,
                                 on_click=on_login),
            ft.OutlinedButton("Logout", icon=ft.Icons.LOGOUT,
                              on_click=on_logout),
        ], spacing=12, wrap=True)

        # --- Section 4: About ---
        about_card = section(
            "About",
            [
                *_about_rows(),
                ft.Divider(),
                ft.Row([check_updates_btn],
                       alignment=ft.MainAxisAlignment.START),
            ],
            icon=ft.Icons.INFO,
            subtitle="Version, engine and updater.")

        # --- Section 5: Danger Zone ---
        danger_zone = ft.Container(
            ft.Column(
                [
                    ft.Row([
                        ft.Icon(ft.Icons.WARNING_AMBER, size=20,
                                color=ft.Colors.ERROR),
                        ft.Column([
                            ft.Text("Danger Zone", size=16,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.ERROR),
                            ft.Text("These actions are irreversible.",
                                    size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                        ], spacing=2, expand=True),
                    ], spacing=8),
                    ft.Row([
                        ft.OutlinedButton(
                            "Reset to Defaults",
                            icon=ft.Icons.DELETE_FOREVER,
                            on_click=on_reset,
                            style=ft.ButtonStyle(
                                color=ft.Colors.ERROR,
                            ),
                        ),
                    ], alignment=ft.MainAxisAlignment.START),
                ],
                spacing=12,
            ),
            padding=20,
            border=ft.Border.all(2, ft.Colors.ERROR),
            border_radius=12,
            bgcolor=ft.Colors.ERROR_CONTAINER
            if hasattr(ft.Colors, "ERROR_CONTAINER")
            else None,
        )

        return ft.Column([
            h1(ft.Icons.SETTINGS, "Settings",
               "Credentials, output mode, updates and environment."),
            api_card,
            settings_error,
            action_buttons,
            mode_card,
            deps_section(page),
            about_card,
            danger_zone,
        ], spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)

    # ------------------------------------------------------------ deps page
    def deps_view(page: ft.Page, header: bool = True):
        import flet as ft
        col = ft.Column(spacing=20, scroll=ft.ScrollMode.AUTO, expand=True)
        sys_txt = ft.Text(f"{platform.system()} {platform.release()} • Python {platform.python_version()}",
                          size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        def render(rows_data=None):
            col.controls.clear()
            # header=False when embedded as a section inside Settings, which
            # already has its own page title and scroll container.
            if header:
                col.controls.append(h1(ft.Icons.BUILD, "Dependencies",
                                       "Engine status for download + convert."))
            col.controls.append(sys_txt)
            if rows_data is None:
                # First build happens before the view is shown, so the sync
                # subprocess probes here are acceptable. Repeated checks go
                # through _recheck (thread) to keep the UI responsive.
                rows_data = deps_mod.check_all()
            rows: list = []
            for d in rows_data:
                rows.append(ft.Row([
                    ft.Icon(ft.Icons.CHECK_CIRCLE if d.ok else ft.Icons.ERROR, size=20,
                            color=ft.Colors.GREEN if d.ok else ft.Colors.ERROR),
                    ft.Column([ft.Text(d.name, size=14, weight=ft.FontWeight.W_500),
                               ft.Text(d.version or d.hint, size=12,
                                       color=ft.Colors.ON_SURFACE_VARIANT)],
                              spacing=2, expand=True),
                    ft.Text("OK" if d.ok else "MISSING", size=12, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.GREEN if d.ok else ft.Colors.ERROR),
                ], spacing=12))
                if not d.ok:
                    rows.append(ft.Container(
                        ft.Row([
                            ft.Icon(ft.Icons.TERMINAL, size=16,
                                    color=ft.Colors.ON_ERROR_CONTAINER),
                            ft.Text(install_cmd_for(d), size=12, selectable=True,
                                    expand=True,
                                    color=ft.Colors.ON_ERROR_CONTAINER),
                        ], spacing=8),
                        bgcolor=ft.Colors.ERROR_CONTAINER,
                        border_radius=8, padding=12))
            async def _recheck(e):
                # check_all spawns subprocesses (ffprobe -version, etc.):
                # offload to a thread so repeated checks never freeze the UI.
                btn = e.control
                btn.disabled = True
                btn.text = "Checking…"
                page.update()
                try:
                    data = await asyncio.to_thread(deps_mod.check_all)
                    render(data)
                except Exception as exc:
                    snack(page, f"Dependency check failed: {exc}", error=True)
                finally:
                    btn.disabled = False
                    btn.text = "Re-check"
                    page.update()

            rows.append(ft.Row([ft.FilledTonalButton("Re-check", icon=ft.Icons.REFRESH,
                                                     on_click=_recheck)],
                               alignment=ft.MainAxisAlignment.START))
            col.controls.append(section("Status", rows, icon=ft.Icons.CHECKLIST,
                                        subtitle="Required tools for download + convert."))
            try:
                page.update()
            except Exception:
                pass

        render()
        return col

    def deps_section(page: ft.Page):
        """Dependencies embedded as a section inside Settings.

        It used to be a top-level tab, which made Settings feel incomplete
        ("where is the rest of it?") and pushed the real page count up to four.
        Folding it in matches how the rest of the app is organised: Settings is
        the place for configuration and environment, and dependency status is
        environment. The inner view owns its own scroll, so nesting it inside
        Settings' scroll container needs the wrapper below to stop it expanding
        to full height and stealing the parent's scroll range.
        """
        inner = deps_view(page, header=False)
        return ft.Container(
            content=ft.Column([inner], spacing=16, tight=True),
            padding=20,
        )

    # ------------------------------------------------------------ dialogs
    # Flet 0.86 has no page.open/page.close: dialogs are shown via overlay.
    def _show_dlg(page, dlg):
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    def _hide_dlg(page, dlg):
        try:
            dlg.open = False
            page.update()
        except Exception:
            pass
        # Best-effort secret wipe + overlay removal so phone/token/code/2FA
        # values don't linger in the widget tree after the dialog closes.
        try:
            content = getattr(dlg, "content", None)
            if content is not None and hasattr(content, "value"):
                content.value = ""
        except Exception:
            pass
        try:
            if dlg in page.overlay:
                page.overlay.remove(dlg)
        except Exception:
            pass

    def _wipe_field(field) -> None:
        try:
            field.value = ""
        except Exception:
            pass

    async def gui_login(page, client) -> bool:
        """Phone/code/2FA dialogs. Returns True on success."""
        import flet as ft
        from telethon import errors as terr
        phone_f = ft.TextField(label="Phone number or bot token",
                               autofocus=True,
                               prefix_icon=ft.Icons.PHONE if hasattr(ft.Icons, "PHONE") else None)
        dlg = ft.AlertDialog(title=ft.Text("Telegram login"),
                             content=phone_f,
                             actions=[ft.TextButton("Cancel"), ft.TextButton("Send code")])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _cancel(e):
            if not fut.done():
                fut.set_result(None)
            _hide_dlg(page, dlg)

        async def _send(e):
            if not fut.done():
                fut.set_result(phone_f.value.strip())
            _hide_dlg(page, dlg)

        dlg.actions[0].on_click = _cancel
        dlg.actions[1].on_click = _send
        _show_dlg(page, dlg)
        phone = await fut
        _wipe_field(phone_f)
        if not phone:
            return False
        is_phone = tg_client.looks_like_phone(phone)
        # Do NOT pre-send code: client.start() sends exactly once; the
        # previous manual send caused double SMS + code invalidation.
        async def _code_dialog() -> str:
            # Telethon's default code_callback is blocking input() — that
            # would freeze the asyncio loop. Always answer with a dialog.
            f = ft.TextField(label="Login code from Telegram", autofocus=True)
            d = ft.AlertDialog(title=ft.Text("Enter the code we just sent"),
                               content=f,
                               actions=[ft.TextButton("Cancel"), ft.TextButton("OK")])
            f4: asyncio.Future = asyncio.get_running_loop().create_future()
            d.actions[0].on_click = lambda e: (f4.set_result("") if not f4.done() else None, _hide_dlg(page, d))

            def _ok(e):
                if not f4.done():
                    f4.set_result(f.value.strip())
                _hide_dlg(page, d)
            d.actions[1].on_click = _ok
            _show_dlg(page, d)
            val = await f4
            _wipe_field(f)
            return val or ""
        try:
            if is_phone:
                await client.start(phone=phone, code_callback=_code_dialog)
            else:
                await client.start(bot_token=phone)
            try:
                me = await client.get_me()
                try:
                    st = load_settings()
                    me_id = str(getattr(me, "id", "") or "")
                    if me_id:
                        st.user_id = me_id
                        save_settings(st)
                except Exception:
                    pass
            except Exception:
                pass
            harden_session_files()
            return True
        except terr.FloodWaitError as exc:
            snack(page, f"Telegram rate-limit: wait {exc.seconds}s.", error=True)
            return False
        except Exception:
            pass
        if not is_phone:
            # Bot tokens never take a login code: don't prompt for one
            # (avoids an unbounded code-guessing loop on tokens).
            snack(page, "Bot login failed. Check the token and try again.", error=True)
            try:
                del phone
            except NameError:
                pass
            return False
        # Fall through: ask code then 2FA password via dialogs (max attempts).
        code = None
        for _attempt in range(3):
            code_f = ft.TextField(label="Login code from Telegram")
            dlg2 = ft.AlertDialog(title=ft.Text("Enter code"), content=code_f,
                                  actions=[ft.TextButton("Cancel"), ft.TextButton("Confirm")])
            fut2: asyncio.Future = asyncio.get_running_loop().create_future()
            dlg2.actions[0].on_click = lambda e: (fut2.set_result(None) if not fut2.done() else None, _hide_dlg(page, dlg2))

            async def _c2(e):
                if not fut2.done():
                    fut2.set_result(code_f.value.strip())
                _hide_dlg(page, dlg2)
            dlg2.actions[1].on_click = _c2
            _show_dlg(page, dlg2)
            code = await fut2
            _wipe_field(code_f)
            if not code:
                try:
                    del phone
                except NameError:
                    pass
                return False
            try:
                await client.sign_in(phone=phone, code=code)
                try:
                    del code
                except NameError:
                    pass
                harden_session_files()
                return True
            except terr.FloodWaitError as exc:
                snack(page, f"Telegram rate-limit: wait {exc.seconds}s.", error=True)
                return False
            except Exception as exc:
                if isinstance(exc, terr.SessionPasswordNeededError):
                    break
                if _attempt >= 2:
                    snack(page, "Login failed: too many wrong codes. Request a new code.", error=True)
                    try:
                        del phone
                    except NameError:
                        pass
                    try:
                        del code
                    except NameError:
                        pass
                    return False
                snack(page, "Wrong code, try again (attempt "
                      f"{_attempt + 2}/3).", error=True)
                continue
        # The loop broke out on SessionPasswordNeededError — fall through to 2FA.
        try:
            pw_f = ft.TextField(label="2FA password", password=True, can_reveal_password=True)
            dlg3 = ft.AlertDialog(title=ft.Text("2-step verification"), content=pw_f,
                                  actions=[ft.TextButton("Cancel"), ft.TextButton("Login")])
            fut3: asyncio.Future = asyncio.get_running_loop().create_future()
            dlg3.actions[0].on_click = lambda e: (fut3.set_result(None) if not fut3.done() else None, _hide_dlg(page, dlg3))

            async def _c3(e):
                if not fut3.done():
                    fut3.set_result(pw_f.value)
                _hide_dlg(page, dlg3)
            dlg3.actions[1].on_click = _c3
            _show_dlg(page, dlg3)
            pw = await fut3
            _wipe_field(pw_f)
            if not pw:
                return False
            try:
                await client.sign_in(password=pw)
            finally:
                try:
                    del pw
                except NameError:
                    pass
            try:
                del phone
            except NameError:
                pass
            try:
                del code
            except NameError:
                pass
            harden_session_files()
            return True
        except Exception:
            snack(page, "Login failed.", error=True)
            return False
        finally:
            try:
                del phone
            except NameError:
                pass

    async def _reset_identity_pin(page, client) -> bool:
        """Deliberate, confirmed escape from an identity-pin mismatch.

        Because get_client() no longer clears the pin when it refuses, a user
        who genuinely switched accounts would otherwise be stuck: every
        attempt re-triggers the same refusal with no way forward. This is the
        ONLY thing that clears the pin, and it is intentionally awkward --
        an explicit dialog naming the risk, so it cannot be triggered by a
        planted session, a stray click, or a retry.

        Returns True if the pin was cleared (caller should retry sign-in).
        """
        ok = await confirm_dialog(
            page,
            "Use a different Telegram account?",
            "This install is pinned to a different Telegram account, so the "
            "saved session was refused.\n\n"
            "Only continue if YOU changed accounts. If you did not, someone "
            "else may have replaced the session file on this computer — "
            "cancelling leaves the pin in place and keeps refusing it.\n\n"
            "Clearing the pin lets the next sign-in be trusted.",
            action="Clear pin and sign in",
            icon=ft.Icons.WARNING_AMBER)
        if not ok:
            return False
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            clear_session()
        except Exception:
            pass
        try:
            st0 = load_settings()
            st0.user_id = None
            save_settings(st0)
        except Exception as exc:
            snack(page, f"Could not clear the account pin: {exc}", error=True)
            return False
        state["client"] = None
        state.pop("client_fp", None)
        state.pop("me_id", None)
        return True

    async def confirm_dialog(page, title: str, body: str,
                             action: str = "Download",
                             icon=ft.Icons.DOWNLOAD) -> bool:
        import flet as ft
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        dlg = ft.AlertDialog(title=ft.Text(title), content=ft.Text(body),
                             icon=ft.Icon(icon),
                             actions=[ft.TextButton("Cancel"), ft.FilledButton(action)])
        dlg.actions[0].on_click = lambda e: (fut.set_result(False) if not fut.done() else None, _hide_dlg(page, dlg))
        dlg.actions[1].on_click = lambda e: (fut.set_result(True) if not fut.done() else None, _hide_dlg(page, dlg))
        _show_dlg(page, dlg)
        return bool(await fut)

    async def update_dialog(page, info) -> str:
        """Update-available dialog. Returns 'later' | 'page' | 'download'."""
        import flet as ft
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _pick(value: str):
            async def _go(e):
                if not fut.done():
                    fut.set_result(value)
                _hide_dlg(page, dlg)
            return _go

        dlg = ft.AlertDialog(
            title=ft.Text("Update available"),
            icon=ft.Icon(ft.Icons.UPDATE),
            content=ft.Text(f"Version v{info.latest_version} is available "
                            f"(you have v{app_version()}).\n\n"
                            "Download the installer now, or open the release page instead."),
            actions=[ft.TextButton("Later", on_click=_pick("later")),
                     ft.TextButton("Open Release Page", on_click=_pick("page")),
                     ft.FilledButton("Download Installer", on_click=_pick("download"))])
        _show_dlg(page, dlg)
        return str(await fut)

    # ------------------------------------------------------------ shell
    async def _main(page: ft.Page):
        page.title = "Telegram Downloader"
        # Rich theme overrides with custom backgrounds for dark/light modes
        dark_theme = ft.Theme(
            color_scheme_seed=SEED,
            scaffold_bgcolor=DARK_BG,
            # Achromatic by design. Every role below is a grey, so hierarchy
            # comes from lightness rather than hue. The two exceptions are
            # `tertiary` (emerald, used for "done") and `error` (red), which
            # must stay distinguishable because they carry meaning. `secondary`
            # matters more than it looks: Flet paints the NavigationRail
            # selected pill and some filled buttons with it, so a coloured
            # secondary puts that hue on every tab the user selects.
            color_scheme=ft.ColorScheme(
                primary="#D7DBE4",
                on_primary="#0F1117",
                primary_container="#2A2F3A",
                on_primary_container="#E6E8EF",
                secondary="#9AA0AE",
                on_secondary="#0F1117",
                secondary_container="#252935",
                on_secondary_container="#D7DBE4",
                tertiary=STATUS_COLORS["done"],
                on_tertiary="#0F1117",
                tertiary_container="#0C3A2A",
                on_tertiary_container="#A7F3D0",
                error=STATUS_COLORS["error"],
                on_error="#0F1117",
                error_container="#4C1D1D",
                on_error_container="#FECACA",
                surface=DARK_SURFACE,
                on_surface="#E6E8EF",
                surface_container_high="#1F2330",
                # Was #B0B0C0 against a #2D2D4A surface -- roughly 4:1, and
                # it is used for every subtitle and hint, so the whole secondary
                # layer was too dim. Lightened for readable contrast.
                on_surface_variant="#A8AEC2",
                outline="#2E3342",
            ),
        )
        light_theme = ft.Theme(
            color_scheme_seed=SEED,
            scaffold_bgcolor=LIGHT_BG,
            color_scheme=ft.ColorScheme(
                primary="#3A3F4B",
                on_primary="#FFFFFF",
                primary_container="#E2E5EC",
                on_primary_container="#1A1D26",
                secondary="#6B7280",
                on_secondary="#FFFFFF",
                secondary_container="#E8EAEF",
                on_secondary_container="#1A1D26",
                tertiary="#047857",
                on_tertiary="#FFFFFF",
                tertiary_container="#D1FAE5",
                on_tertiary_container="#065F46",
                error="#DC2626",
                on_error="#FFFFFF",
                error_container="#FEE2E2",
                on_error_container="#991B1B",
                surface=LIGHT_SURFACE,
                on_surface="#14161D",
                surface_container_high="#EFEFF3",
                on_surface_variant="#545A6B",
                outline="#C9CBD4",
            ),
        )
        page.theme = light_theme
        page.dark_theme = dark_theme
        page.padding = 0
        page.window.min_width = 800
        page.window.min_height = 600
        apply_theme(page, load_settings().theme)

        # -- Theme toggle --------------------------------------------------
        def _toggle_theme(e):
            st = load_settings()
            new_theme = "light" if (st.theme or "dark") == "dark" else "dark"
            st.theme = new_theme
            save_settings(st)
            apply_theme(page, new_theme)
            try:
                theme_btn.icon = (ft.Icons.LIGHT_MODE
                                  if new_theme == "dark" else ft.Icons.DARK_MODE)
                theme_btn.tooltip = ("Switch to light mode"
                                     if new_theme == "dark"
                                     else "Switch to dark mode")
            except Exception:
                pass
            page.update()

        theme_btn = ft.IconButton(
            icon=ft.Icons.LIGHT_MODE,
            tooltip="Switch to light mode",
            on_click=_toggle_theme,
            icon_size=20,
            # Sits on the blue app bar, so it takes the bar's ink rather than
            # the scheme's ON_PRIMARY (which is near-black on the grey primary
            # and would have been invisible here anyway). IconButton spells it
            # icon_color, not color.
            icon_color=APPBAR_FG,
        )
        # Sync initial icon with current theme
        _st0_icon = load_settings()
        if (_st0_icon.theme or "dark") == "light":
            theme_btn.icon = ft.Icons.DARK_MODE
            theme_btn.tooltip = "Switch to dark mode"

        # -- AppBar --------------------------------------------------------
        # Explicit APPBAR_BG/FG rather than PRIMARY/ON_PRIMARY: PRIMARY is the
        # achromatic scheme colour, so using it here is what made the title bar
        # white.
        #
        # The mark is the APP's logo, not the Telegram paper plane.
        # assets/icon.svg ships inside the package, so the title bar
        # shows the real artwork in every install. It is loaded as a
        # self-contained data URI (see app_logo_src) because ft.app()
        # here accepts no assets_dir to serve the file from. If the
        # asset cannot be read for any reason, fall back to a drawn
        # square + download arrow that matches the SVG -- a broken
        # image box is worse than a close copy.
        logo_src = app_logo_src()
        if logo_src is not None:
            app_logo = ft.Image(
                src=logo_src, width=24, height=24,
                fit=ft.BoxFit.CONTAIN,
            )
        else:
            app_logo = ft.Container(
                width=24, height=24,
                border_radius=0,      # square, not a lozenge
                bgcolor=APPBAR_FG,
                alignment=ft.Alignment.CENTER,
                padding=0,
                content=ft.Icon(ft.Icons.DOWNLOAD, color=APPBAR_BG, size=17),
            )
        app_bar = ft.AppBar(
            leading=ft.Container(
                app_logo, width=24, height=24,
                alignment=ft.Alignment.CENTER,
                padding=0, margin=ft.Margin(left=10),
            ),
            leading_width=44,
            title=ft.Text(
                "Telegram Downloader",
                size=14,
                weight=ft.FontWeight.W_600,
                color=APPBAR_FG,
            ),
            center_title=False,
            bgcolor=APPBAR_BG,
            color=APPBAR_FG,
            elevation=0,
            actions=[theme_btn],
        )

        # -- Body container ------------------------------------------------
        # Centred, width-capped shell. Previously the body stretched edge to
        # edge, so on a 1280px window every form field ran the full width and
        # ~60% of the screen was dead space. max_width keeps line length
        # readable and puts the content on a predictable left margin.
        body_content = ft.Container(expand=True)
        body = ft.Container(
            content=ft.Container(
                content=body_content,
                width=860,
                expand=True,
                margin=ft.Margin(0, 0, 0, 0),
                padding=ft.Padding(32, 32, 32, 32),
            ),
            expand=True,
            alignment=ft.Alignment.TOP_CENTER,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
        )

        # deps_view used to be a fourth tab. It now lives inside Settings as a
        # section (deps_section), so the top tabs are Add / Queue / Settings.
        views = [downloads_view(page), queue_view(page), settings_view(page)]

        # -- Top navigation ---------------------------------------------------
        # Tabs sit across the TOP, not down the side, and Dependencies is gone
        # from the tab list (it is a Settings section now). This matches
        # simple-yt-downloader's StackSwitcher-in-the-headerbar layout and
        # gives the content the full window width, which matters because the
        # Queue rows are the widest thing in the app and were previously
        # sharing it with a 72-200px rail.
        # Built from primitives on purpose. flet 0.86.5's tab controls do not fit:
        # ft.Tabs owns its own content and its tabs are positional children, so
        # _goto() could not drive selected_index the way refresh_queue() and the
        # first-run nudge need; ft.TabBar is header-only but has neither
        # selected_index nor on_change. A Row of chips keeps the content ours,
        # gives us the selected state to style, and does not depend on which
        # of those three APIs the pinned Flet happens to ship.
        _tab_chips: list = []

        def _make_chip(idx: int, label: str, icon):
            def _pick(e):
                _goto(idx)
            chip = ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(icon, size=18),
                        ft.Text(label, size=14, weight=ft.FontWeight.W_600),
                    ],
                    spacing=8,
                    tight=True,
                ),
                padding=ft.Padding(14, 9, 14, 9),
                border_radius=10,
                on_click=_pick,
                # Identity is carried on the chip, not implied by its position in
                # _tab_chips. queue_chip is built FIRST (refresh_queue needs a
                # handle on it) but renders SECOND, so enumerate() order is
                # [Queue, Add, Settings] while the screen shows
                # [Add, Queue, Settings] -- clicking Add lit up Queue.
                data=idx,
            )
            _tab_chips.append(chip)
            return chip

        queue_chip = _make_chip(1, "Queue", ft.Icons.QUEUE_OUTLINED)
        tab_bar = ft.Row(
            [
                _make_chip(0, "Add", ft.Icons.LINK_OUTLINED),
                queue_chip,
                _make_chip(2, "Settings", ft.Icons.SETTINGS_OUTLINED),
            ],
            spacing=8,
            tight=True,
        )

        # Expose the Queue chip so refresh_queue can put the pending count on it.
        queue_header_txt["tab"] = queue_chip
        # Which view is on screen. Guards re-taps of the active chip.
        _current_idx = -1

        def _set_selected_index(idx: int):
            # Selected chip reads as a filled surface; the rest stay flat.
            # Matched on chip.data, never enumerate order -- see _make_chip.
            #
            # animate_opacity is NOT used here on purpose: it is the cheapest
            # property to composite, but it only fades the WHOLE chip, so an
            # unselected chip would briefly ghost at partial opacity instead of
            # going cleanly flat. bgcolor/border are painted every frame either
            # way at this size (3 chips), so the cost is irrelevant and the
            # direct property change is the honest one. Duration is short on
            # purpose: this is a selection indicator, not a transition the user
            # should wait out.
            for chip in _tab_chips:
                on = (chip.data == idx)
                chip.bgcolor = (ft.Colors.SECONDARY_CONTAINER if on
                                else ft.Colors.TRANSPARENT)
                chip.content.controls[0].color = (ft.Colors.ON_SECONDARY_CONTAINER
                                                  if on
                                                  else ft.Colors.ON_SURFACE_VARIANT)
                chip.content.controls[1].color = (ft.Colors.ON_SECONDARY_CONTAINER
                                                  if on
                                                  else ft.Colors.ON_SURFACE_VARIANT)
                chip.border = (ft.Border.all(1, ft.Colors.OUTLINE) if on
                               else ft.Border.all(1, ft.Colors.TRANSPARENT))

        def on_nav(idx: int):
            # `nonlocal` is required, not decorative. Assigning _current_idx
            # here makes Python treat it as a LOCAL of on_nav, so the read on
            # the line above raised UnboundLocalError ("cannot access local
            # variable '_current_idx' where it is not associated with a value")
            # the first time any tab was clicked.
            nonlocal _current_idx
            # Re-tapping the active tab used to rebuild the view (Queue
            # re-rendered every card) and flash. No-op instead.
            if idx == _current_idx:
                return
            _current_idx = idx
            _set_selected_index(idx)
            body_content.content = views[idx]
            # ONE update per switch. refresh_queue()/refresh_settings_hint()
            # each end with their own page.update(), so updating here too meant
            # serialising the entire control tree twice per click.
            if idx == 1:
                refresh_queue()
            elif idx == 2:
                refresh_settings_hint()
            else:
                page.update()

        def _apply_layout():
            # Padding only. The tabs span the full width at every size, so the
            # old narrow/wide branch (rail vs bottom bar) is gone entirely --
            # there is nothing left for it to decide.
            if page.width < 600:
                padding = 12
            elif page.width < 900:
                padding = 20
            else:
                padding = 28
            body.padding = padding

        def on_resize(e):
            _apply_layout()
            page.update()

        page.on_resize = on_resize

        def _goto(idx: int):
            on_nav(idx)

        nav["goto"] = _goto

        # -- Bottom bar: download location ---------------------------------
        # Pinned to the bottom of the window so the folder can be changed in
        # one click, the way simple-yt-downloader does it. It was previously
        # only editable inside Settings, so changing where downloads land
        # meant navigating to Settings first -- and the field most likely to
        # need changing is the one furthest from view.
        dl_path_txt = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                              expand=True, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)

        def _current_dl_label() -> str:
            try:
                p = ensure_download_dir(load_settings())
                home = str(Path.home())
                s = str(p)
                return "~" + s[len(home):] if s.startswith(home + "/") else s
            except Exception:
                return "(not set)"

        def _refresh_dl_label():
            dl_path_txt.value = _current_dl_label()

        async def _pick_download_dir(e):
            picker = _page_service(page, ft.FilePicker)
            try:
                chosen = await picker.get_directory_path(
                    dialog_title="Choose download folder")
            except Exception as exc:
                snack(page, f"Could not open folder picker: {exc}", error=True)
                return
            if not chosen:
                return
            # Validate before storing: config.ensure_download_dir() rejects
            # paths inside the config dir, filesystem roots, and paths with
            # shell metacharacters, and the sidebar label must never show a
            # folder that would then fail at download time.
            try:
                st = load_settings()
                st.download_dir = str(chosen)
                ensure_download_dir(st)
                save_settings(st)
            except Exception as exc:
                snack(page, f"Cannot use this folder: {exc}", error=True)
                return
            _refresh_dl_label()
            snack(page, "Download folder updated.")
            try:
                page.update()
            except Exception:
                pass

        dl_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.FOLDER_OUTLINED, size=18,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    dl_path_txt,
                    ft.TextButton("Change", on_click=_pick_download_dir),
                ],
                spacing=10,
            ),
            padding=ft.Padding(16, 8, 16, 8),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
        )
        _refresh_dl_label()

        # -- Page shell: AppBar + layout -----------------------------------
        page.appbar = app_bar

        # First-run nudge: open Settings when API/folder missing.
        st0 = load_settings()
        start_idx = 2 if (not st0.api_id or not st0.api_hash or not st0.download_dir) else 0
        _set_selected_index(start_idx)
        body_content.content = views[start_idx]
        _apply_layout()

        # Tabs on top, download location at the bottom, content between.
        page.add(ft.Column(
            [
                ft.Container(tab_bar, padding=ft.Padding(32, 12, 32, 4)),
                body,
                dl_bar,
            ],
            expand=True,
            spacing=0,
        ))

    import flet as ft
    # `view` exists so the GUI can be served over HTTP on a headless machine
    # (Codespaces, CI, a container): without it flet opens a desktop window
    # and dies when there is no display. Left None, behaviour is unchanged.
    if view is not None:
        ft.app(target=_main, port=port, view=view)
    elif port:
        ft.app(target=_main, port=port)
    else:
        ft.app(target=_main)
