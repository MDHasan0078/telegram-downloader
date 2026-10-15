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
from . import ui
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

# -- Layout metrics ----------------------------------------------------------
# Every chrome bar AND the page body are capped at this width, so the nav, the
# content and the footer all sit on ONE horizontal axis.
#
# This replaces the old arrangement, which had three different axes stacked on
# top of each other: a full-bleed tab strip hugging the far left, an 860px body
# column floating in the centre, and a full-bleed footer. Nothing lined up, so
# at any width the app read as a web form rather than an application.
#
# There is deliberately NO max width now. A centred cap is what made the app
# look like a phone layout stretched onto a desktop: on any normal monitor the
# content occupied a fraction of the window with dead black either side. The
# content now fills the window and the only constraint is the gutter, so the
# window width is actually used.
SHELL_PAD = 24

# Readable measure for a single-line text input. Cards and lists span the full
# window; the inputs inside them do not, because an API id is ~10 characters
# and a field 1400px wide makes it harder to find, not easier.
FIELD_MAX = 520


# -- Status colors (designed for dark-first palette, good contrast on dark bg) --
# The one place hue is allowed to carry meaning. These are the only saturated
# things on screen while a download runs; everything structural is achromatic.
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
# Flat fills, no elevation. A snackbar is the transient voice of the app, so it
# uses the same dark surfaces as everything else rather than introducing a
# brighter band that flashes against the navy scaffold.
SNACK_SUCCESS_BG = "#12301F"
SNACK_SUCCESS_FG = "#7FD8A0"
SNACK_ERROR_BG   = "#3A1216"
SNACK_ERROR_FG   = "#FFB4AB"
SNACK_INFO_BG    = "#182236"
SNACK_INFO_FG    = "#93A2BD"

# -- Error / hint box styling --
ERROR_BOX_BG     = "#3A1216"
ERROR_BOX_BORDER = "#E5484D"
HINT_BOX_BG      = "#182236"
HINT_BOX_BORDER  = "#24324D"

_queue_widgets = {}  # item_id -> {card, progress_bar, pct_pill, detail_txt, ...}
MODE_LABELS = {"1": "Original", "2": "Fast", "3": "Max"}
# What each mode actually does, in words. The Settings card used to show only
# "Default: Original", which told the user nothing -- they could not tell
# whether Original was best, worst, or merely the default.
#
# Sourced from converter.ffmpeg_to_mp4: when the source is already H.264/AAC it
# is remuxed with -c copy regardless of mode, so the mode only actually changes
# anything when the source has to be re-encoded.
MODE_BLURB = {
    "1": "Keep the file exactly as Telegram sent it. No conversion, so "
         "nothing is ever re-encoded.",
    "2": "Convert to MP4 with a fast encoder preset. Smaller file, quicker to "
         "process.",
    "3": "Convert to MP4 with a better compression preset. Smallest file, "
         "slower to process.",
}


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

    def _copy_clipboard(page, text: str) -> None:
        """Copy text to the clipboard, falling back to a selection the user can
        copy by hand.

        Flet's clipboard service is a no-op on some desktop builds and the web
        view, and silently doing nothing after the user clicked "Copy" is worse
        than useless. If the service is unavailable the text is selected in a
        dialog so Ctrl+C still works.
        """
        async def _do():
            try:
                svc = _page_service(page, ft.Clipboard)
                await svc.set_clipboard(text)
                snack(page, "Command copied.")
                return
            except Exception:
                pass
            dlg = ft.AlertDialog(
                title=ft.Text("Copy this command"),
                content=ft.TextField(value=text, multiline=True,
                                     read_only=True),
                actions=[ft.TextButton("Close",
                                       on_click=lambda e: _hide_dlg(page, dlg))])
            _show_dlg(page, dlg)
        page.run_task(_do)

    def install_cmd_for(dep) -> str:
        return dep.install_for_current_os()

    # ------------------------------------------------------------ design kit
    # Type scale. Every heading used to be oversized (24px page titles, 26px
    # icon tiles) with almost no steps between levels, so the whole app read as
    # zoomed in. These are deliberate ratios: 19 / 14.5 / 13 / 11.5.
    def h1(icon, title: str, subtitle: str | None = None):
        """Page header: small icon tile + title + optional subtitle."""
        title_col = [ft.Text(title, size=16, weight=ft.FontWeight.W_700)]
        if subtitle:
            title_col.append(ft.Text(subtitle, size=12,
                                     color=ft.Colors.ON_SURFACE_VARIANT))
        return ft.Row([
            ft.Container(
                ft.Icon(icon, size=13, color=ft.Colors.ON_SURFACE_VARIANT),
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                border_radius=7, padding=6),
            ft.Column(title_col, spacing=1, expand=True),
        ], spacing=10)

    def mono(text, size=11, color=None, weight=None):
        """Technical value in a monospace face: paths, versions, commands.

        Stitch's system reserves mono for machine-level values (JetBrains
        Mono) and sans for prose, so a command or a version reads differently
        from a sentence at a glance instead of blending into the paragraph.
        'monospace' is a generic family Flutter resolves on every desktop
        target, so this needs no font download at runtime.
        """
        return ft.Text(text, size=size, font_family="monospace",
                       color=color or ft.Colors.ON_SURFACE,
                       weight=weight)

    def badge(text, tone="neutral"):
        """Small caps status chip, e.g. OK / MISSING / a mode name."""
        fg, bg = {
            "neutral": (ft.Colors.ON_SURFACE_VARIANT,
                        ft.Colors.SURFACE_CONTAINER_HIGH),
            "ok": (STATUS_COLORS["done"], "#0C2A20"),
            "bad": (STATUS_COLORS["error"], "#3A1416"),
            "accent": ("#89CEFF", "#0B2438"),
        }.get(tone, (ft.Colors.ON_SURFACE_VARIANT,
                     ft.Colors.SURFACE_CONTAINER_HIGH))
        return ft.Container(
            ft.Text(text, size=10.5, weight=ft.FontWeight.W_600,
                    color=fg, font_family="monospace"),
            bgcolor=bg, border_radius=4, padding=ft.Padding(7, 3, 7, 3))

    def group(title: str, body: list, icon=None, subtitle: str | None = None,
              action=None, *, card: bool = False):
        """A titled group of controls. Bare by default -- no box, no divider.

        This replaces the old `section()`, which wrapped every group in its own
        bordered card. That is the single change responsible for this GUI
        reading as broken: three or four full-width boxes stacked on a
        background, each with its own title and its own hairline, so nothing on
        the page had a clear primary area and every group looked equally
        important. The reference states the rule in style.py:

            One card language for video rows, playlist rows, loading rows, and
            the download-path bar.

        Everything that is not a list item sits directly on the scaffold under
        a muted label. See simple-yt-downloader's `settings_view._section`,
        which is three lines long and returns a bare Gtk.Label with the
        `dim-label` class -- no frame anywhere.

        `card=True` opts back in, and is used only for the About and
        Dependencies groups, which hold tabular data and are the two places the
        reference itself puts a border.
        """
        head = [ft.Text(title, size=11.5, weight=ft.FontWeight.W_600,
                        color=ft.Colors.ON_SURFACE_VARIANT)]
        if subtitle:
            head.append(ft.Text(subtitle, size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT))
        # expand=True has to be on the thing that is a DIRECT child of the
        # header Row, or the action does not get pushed to the right edge --
        # a Row nested inside an expanding Row does not inherit the expansion.
        title_col = ft.Column(head, spacing=1, expand=True)
        if icon is not None:
            title_row = ft.Row([
                ft.Icon(icon, size=13, color=ft.Colors.ON_SURFACE_VARIANT),
                title_col,
            ], spacing=7, expand=True)
        else:
            title_row = title_col
        title_row = ft.Row([title_row, action if action is not None
                            else ft.Container()],
                           vertical_alignment=ft.CrossAxisAlignment.CENTER)
        # STRETCH is what makes inputs fill the group. Without it Flet sizes a
        # TextField to its content, so every form stopped at ~55% of the width
        # and left a ragged dead strip on the right.
        col = ft.Column([title_row, *body], spacing=12,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        if not card:
            return col
        return ft.Container(
            col,
            padding=12,
            bgcolor=ft.Colors.SURFACE,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=12)

    def labelled(label: str, control, hint: str | None = None,
                 expand: bool = False):
        """Label row (name left, hint right) above a control that fills.

        Two changes from the old forms. The label moved out of the field border
        so it can share a row with a right-aligned hint -- that is where the
        generated design puts the reference ("telegram.org/apps -> App api_id")
        and "128-bit hex string", which tells the user what to type before they
        type it. And the control stretches, because Flet otherwise sizes a
        TextField to its content and every field stopped at ~60% of the card.

        `expand=True` stacks the hint UNDER the label and lets the control fill
        its slot. That is what a field needs when it is half of a side-by-side
        pair: the hint has nowhere useful to go beside a sibling, and the
        caller has already divided the width, so capping again would leave the
        row ragged.
        """
        if expand:
            head = ft.Column(
                [ft.Text(label, size=11, weight=ft.FontWeight.W_500,
                         color=ft.Colors.ON_SURFACE_VARIANT)]
                + ([ft.Text(hint, size=11, font_family="monospace",
                            color=ft.Colors.ON_SURFACE_VARIANT)]
                   if hint else []),
                spacing=1)
        else:
            head = ft.Text(label, size=11, weight=ft.FontWeight.W_500,
                           color=ft.Colors.ON_SURFACE_VARIANT)
            if hint:
                head = ft.Row([head, ft.Text(hint, size=11,
                                             font_family="monospace",
                                             color=ft.Colors.ON_SURFACE_VARIANT)])
        # Cap the control. A text input stretched across a 1400px window is as
        # unusable as one that stopped at 60% of its card -- just worse in the
        # other direction, because the value is a short id and the eye has to
        # travel to find it. expand=True opts out: see above.
        if (not expand and hasattr(control, "width")
                and getattr(control, "width", None) is None):
            try:
                control.width = FIELD_MAX
            except Exception:
                pass
        # STRETCH when the caller has sized the slot, START otherwise. STRETCH
        # forces children to fill the column and silently overrides the width
        # set above -- which is why the cap had no effect until this split.
        col = ft.Column([head, control], spacing=5,
                        horizontal_alignment=(ft.CrossAxisAlignment.STRETCH
                                              if expand
                                              else ft.CrossAxisAlignment.START))
        if expand:
            # Without this the Column takes its intrinsic width and a Row of two
            # of them hands each child only what it asked for, so the pair
            # clusters at the left instead of splitting the row.
            col.expand = True
        return col

    def _bar(inner, *, bgcolor=None, top_border=False, bottom_border=False,
             pad_y=None):
        """Full-bleed chrome bar whose content shares the body's gutter.

        This is the alignment fix. Nav and footer used to be full-bleed while
        the body was a separate centred column, so nothing lined up. Everything
        goes through _bar and therefore shares one axis -- and nothing is
        capped, so the bar's contents start where the body's do.
        """
        side = ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)
        return ft.Container(
            content=inner,
            bgcolor=bgcolor,
            padding=ft.Padding(SHELL_PAD, pad_y, SHELL_PAD, pad_y) if pad_y is not None else None,
            border=(ft.Border.only(top=side) if top_border else
                    ft.Border.only(bottom=side) if bottom_border else None),
        )

    def _grow(control, flex: int = 1):
        """Mark a Row child as taking the leftover width.

        A Row child that neither expands nor sets a width is laid out at zero
        width in flet 0.86.5. The Row still reserves the child's correct HEIGHT,
        so the failure is silent and looks like a layout bug rather than a
        missing one: on the Add tab the URL field and all three of its buttons
        collapsed to nothing width and the page showed a ~157px blank band
        between the title and the output-mode toggle. Every other visible Row
        here works only because one of its children already expands.

        Verified with a minimal repro: expand=True on the child renders it,
        the same child without expand renders nothing at the same height.
        """
        control.expand = flex
        return control

    def _ink_button_style():
        """ButtonStyle that forces a neutral outline button to render in ink.

        An ft.OutlinedButton inherits the scheme primary, so every "secondary"
        action came out blue-on-blue and competed with the filled primary
        button beside it. Three blue buttons on one row reads as three primary
        actions. Forcing the foreground (and icon) to the muted ink colour puts
        the hierarchy back: one accent action, everything else quiet.
        """
        return ft.ButtonStyle(
            color=ft.Colors.ON_SURFACE_VARIANT,
            icon_color=ft.Colors.ON_SURFACE_VARIANT,
            side=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
        )

    def pending_button(label: str, icon, handler, kind="tonal"):
        """Button that disables itself and spins while its async handler runs.

        Every long action in the app (verify credentials, login, check for
        updates, recheck dependencies) previously gave the user NOTHING: the
        button stayed live and the window looked frozen for several seconds, so
        people clicked again. This is the fix -- immediate, unambiguous feedback.
        """
        cls = {"tonal": ft.FilledTonalButton, "filled": ft.FilledButton,
               "outline": ft.OutlinedButton}.get(kind, ft.FilledTonalButton)
        btn = cls(label, icon=icon, on_click=handler,
                  **({"style": _ink_button_style()} if kind == "outline" else {}))

        async def _run(e):
            btn.disabled = True
            btn.content = ft.Row(
                [ft.ProgressRing(width=15, height=15, stroke_width=2),
                 ft.Text("Working…", size=12)],
                spacing=8, tight=True)
            try:
                btn.update()
            except Exception:
                pass
            try:
                await handler(e)
            finally:
                btn.disabled = False
                btn.content = ft.Text(label, size=12)
                try:
                    btn.update()
                except Exception:
                    pass

        btn.on_click = _run
        return btn

    def info_row(icon, label: str, value: str, trailing=None):
        row = [ft.Icon(icon, size=14, color=ft.Colors.PRIMARY),
               ft.Column([ft.Text(label, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                          ft.Text(value, size=13)], spacing=2, expand=True)]
        if trailing is not None:
            row.append(trailing)
        return ft.Row(row, spacing=12)

    def info_row_ctrl(icon, label: str, value_ctrl, trailing=None):
        """Info row bound to a live control (for settings hints that refresh)."""
        row = [ft.Icon(icon, size=14, color=ft.Colors.PRIMARY),
               ft.Column([ft.Text(label, size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                          value_ctrl], spacing=2, expand=True)]
        if trailing is not None:
            row.append(trailing)
        return ft.Row(row, spacing=12)

    def _row_note(icon, color, text: str, *, mono: bool = False):
        """A one-line outcome note inside a queue card.

        Deliberately not error_box(). That helper paints a full-width saturated
        fill with a hard border, and putting one inside a card produced a red
        panel nested in a card -- the failure message outshouted the filename
        and the progress history it sits under. The state icon already carries
        the colour; the text needs to be readable, not loud.
        """
        return ft.Row([
            ft.Icon(icon, size=13, color=color),
            ft.Text(text, size=11, expand=True,
                    font_family="monospace" if mono else None,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def pct_pill(text: str):
        """Percentage on an active row, as quiet monospace text.

        This was a solid accent-filled pill in the top-right of every active
        card -- the single loudest element on the row, restating what the
        progress bar immediately below it already shows to better precision. The
        reference moves the numbers off the bar and into the status line for
        exactly this reason. Monospace keeps the digits from jittering as the
        value ticks.
        """
        return ft.Text(text, size=11.5, font_family="monospace",
                       color=ft.Colors.ON_SURFACE_VARIANT)

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
            ft.Row([ft.Icon(icon, size=13, color=fg),
                    ft.Text(text, size=11, weight=ft.FontWeight.W_500, color=fg)],
                   spacing=4, tight=True),
            bgcolor=bg, border_radius=8, padding=6)

    def error_box(message: str):
        _side = ft.BorderSide(1, ERROR_BOX_BORDER)
        return ft.Container(
            ft.Row([ft.Icon(ft.Icons.ERROR, size=13,
                            color=SNACK_ERROR_FG),
                    ft.Text(message, size=11, expand=True, selectable=True,
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
                ft.Icon(ft.Icons.INFO_OUTLINE, size=13,
                        color=HINT_BOX_BORDER),
                ft.Text(message, size=11, expand=True,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ], spacing=8),
            bgcolor=HINT_BOX_BG,
            border_radius=10, padding=10,
            border=ft.Border(top=_side, right=_side, bottom=_side, left=_side))

    def empty_state(icon, title: str, subtitle: str,
                    action_label: str | None = None,
                    action_icon=None, on_action=None):
        # This was a 56px icon with a 22px title and 48px of padding, so an
        # empty list occupied ~500px of vertical space and read as a landing
        # page rather than "nothing here yet". A placeholder should be quieter
        # than the content it stands in for, not louder.
        controls = [
            ft.Container(
                ft.Icon(icon, size=14, color=ft.Colors.ON_SURFACE_VARIANT),
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                border_radius=8, padding=8),
            ft.Text(title, size=12, weight=ft.FontWeight.W_600),
            ft.Text(subtitle, size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                    text_align=ft.TextAlign.CENTER),
        ]
        if action_label and on_action is not None:
            controls.append(ft.Container(
                ft.TextButton(action_label, icon=action_icon,
                              on_click=on_action),
                margin=0))
        # expand=True, because this Container is a child of a ListView. A list
        # child that neither expands nor sets a width is laid out at ZERO width
        # and paints nothing -- the empty queue showed a bare toolbar over an
        # empty screen even though this state was being appended the whole time.
        # It was invisible for the same reason the Add tab's input row was.
        return ft.Container(
            ft.Column(controls, spacing=8,
                      alignment=ft.MainAxisAlignment.CENTER,
                      horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=20, expand=True)

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
            ft.Icon(icon, size=14, color=fg),
            bgcolor=bg, border_radius=14, padding=6)

    # ------------------------------------------------------------ ADD page
    def downloads_view(page: ft.Page):
        url_count_txt = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                                weight=ft.FontWeight.W_500)

        def _on_url_change(e):
            text = url_box.value or ""
            urls = _re_module.findall(r'https?://t\.me/\S+', text)
            n = len(urls)
            url_count_txt.value = f"{n} URL{'s' if n != 1 else ''} detected" if n else ""

        # NO expand=True. min_lines/max_lines already give the field a definite
        # height, and expand put it in a Column whose height is unbounded (the
        # page Column scrolls). Flutter refuses to lay that out: the field and
        # everything beside it in the same Row vanished, leaving a ~190px blank
        # gap and a view with no visible input. Verified against a minimal
        # repro -- same tree with expand removed renders correctly.
        url_box = ft.TextField(
            label="Telegram link(s)",
            hint_text="https://t.me/channel/123\n(one per line)",
            prefix_icon=ft.Icons.LINK,
            multiline=True, min_lines=5, max_lines=10,
            text_size=13, content_padding=11,
            border_radius=12,
            on_change=_on_url_change)
        preview_list = ft.ListView(spacing=12, expand=True)
        status_txt = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                             expand=True)
        status_icon = ft.Icon(ft.Icons.INFO_OUTLINE, size=13,
                              color=ft.Colors.ON_SURFACE_VARIANT)
        # The whole row hides when there is nothing to say. Hiding only the icon
        # left an empty Text still holding a line, so an idle Add tab showed a
        # blank band between the input and the output-mode toggle for no reason.
        status_row = ft.Row([status_icon, status_txt], spacing=8,
                            visible=False)

        def _set_status(text: str):
            """Set the status line, hiding the row entirely when it is empty.

            One writer, so no call site has to remember to clear both the text
            and the row that holds it.
            """
            status_txt.value = text or ""
            status_row.visible = bool(text)
            try:
                page.update()
            except Exception:
                pass
        count_txt = ft.Text("No previews yet",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                            weight=ft.FontWeight.W_500)
        selected_txt = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                               expand=True, weight=ft.FontWeight.W_500)
        fetch_btn = ft.FilledButton("Fetch titles", icon=ft.Icons.SEARCH,
                                    height=48, style=ft.ButtonStyle(
                                        text_style=ft.TextStyle(
                                            weight=ft.FontWeight.BOLD)))
        dl_btn = ft.FilledButton("Download selected", icon=ft.Icons.DOWNLOAD,
                                 disabled=True, height=48, style=ft.ButtonStyle(
                                     text_style=ft.TextStyle(
                                         weight=ft.FontWeight.BOLD)))
        fetch_label = ft.Text("Fetching titles...", size=12,
                              color=ft.Colors.PRIMARY,
                              weight=ft.FontWeight.W_600, visible=False)
        bar = ft.ProgressBar(visible=False, bar_height=6)
        fetch_row = ft.Row([fetch_label, bar], spacing=8, visible=False)

        # --- Mode selector ------------------------------------------------
        # A linked toggle, not three filled cards.
        #
        # The three-card version gave the OUTPUT MODE the same visual weight as
        # the URL textarea -- the actual primary input -- by making it three
        # large bordered blocks with a 2px accent border on the selected one.
        # That is the card-soup mistake again, in a place where it hurts most:
        # a secondary choice rendered as the loudest thing on the page.
        #
        # The reference does the opposite. From style.py:
        #     box.linked > button { background-color: transparent }
        #     box.linked > button:checked { background-color: @accent }
        # One row, the checked segment filled, everything else flat. The
        # explanation moves under it as a caption rather than inside each card.
        _MODE_OPTS = [("1", "Original"), ("2", "Fast"), ("3", "Max")]
        _MODE_DESC = {
            "1": "Raw file, fastest, no conversion.",
            "2": "Compressed MP4, veryfast, smaller.",
            "3": "Best quality compressed, smallest.",
        }
        mode_blurb_txt = ft.Text(_MODE_DESC.get(state.get("mode", "1"), ""),
                                 size=11, expand=True,
                                 color=ft.Colors.ON_SURFACE_VARIANT)

        def _apply_mode(val: str):
            """Single writer for the output mode.

            Everything that changes the mode -- the toggle, the Settings hint
            refresh, and the danger-zone reset -- goes through here, so the
            toggle's painted state can never drift from the stored setting.
            """
            state["mode"] = val
            s = load_settings()
            s.output_mode = val
            save_settings(s)
            mode_blurb_txt.value = _MODE_DESC.get(val, "")
            refresh_settings_hint()

        def _on_mode_pick(val):
            _apply_mode(val)
            try:
                page.update()
            except Exception:
                pass

        mode_toggle = ui.linked_toggle(
            _MODE_OPTS, state.get("mode", "1"), _on_mode_pick)

        # Reset (Settings -> danger zone) drives the toggle through this handle
        # rather than poking a hidden control's `selected` list. The old
        # SegmentedButton it replaced was visible=False and never read by
        # anything else, so resetting the setting left the visible cards showing
        # the old mode until the view was rebuilt.
        state["mode_set"] = _apply_mode

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
            _set_status(f"Fetching {len(urls)} title(s)...")
            fetch_btn.disabled = True
            page.update()
            try:
                c = await get_client(page)
                if not await c.is_user_authorized():
                    ok = await gui_login(page, c)
                    if not ok:
                        _set_status("Login cancelled.")
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
                    _set_status(f"Fetching: {_sanitize_display(u)}")
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
                _set_status((f"Found {n_ok}/{len(urls)} downloadable. "
                                    f"Tick + edit titles, then Download selected."))
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
                        _set_status("Account pin cleared — sign in again.")
                        snack(page, "Account pin cleared. Sign in with the "
                                    "account you want to use.")
                        page.update()
                        return
                    _set_status(msg)
                    snack(page, msg, error=True)
                    page.update()
                    return
                raise
            except Exception as exc:
                _set_status(f"Fetch failed: {_sanitize_display(exc)}")
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
                            ft.Row([ft.Icon(ft.Icons.DESCRIPTION, size=13,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                    ft.Text(r["ext"].upper(), size=11,
                                            weight=ft.FontWeight.W_600,
                                            color=ft.Colors.ON_SURFACE_VARIANT)],
                                   spacing=4, tight=True),
                            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                            border_radius=6, padding=4),
                        ft.Container(
                            ft.Row([ft.Icon(ft.Icons.STORAGE, size=13,
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
            _set_status("")
            page.update()

        fetch_btn.on_click = on_fetch
        dl_btn.on_click = on_download
        render_previews()

        return ft.Column([
            h1(ft.Icons.DOWNLOAD, "Add downloads",
               "Paste t.me links, fetch titles, then queue downloads."),

            # The textarea IS the card.
            #
            # This view used to open with a 20px-padded SURFACE_CONTAINER_LOW
            # slab holding a section header, the textarea, an output-mode
            # picker and three buttons -- then a second identical slab for the
            # preview and a third for two buttons. Three boxes of equal weight
            # on an empty page, with the only thing the user came here to do
            # (paste a link) buried in the middle of the first one.
            #
            # Now there is no card around the input at all: the TextField's own
            # border is the frame, which is what the reference does
            # (`textview.url-input { border-color: ... }` with nothing wrapped
            # around it). Wrapping it in ui.card() put a border inside a border
            # and read as two competing boxes. The primary action sits directly
            # beside the input; the detected-URL count, the mode toggle and the
            # secondary actions sit on the bare scaffold under muted labels,
            # exactly how the reference lays out its settings form.
            ft.Row(
                [
                    _grow(ft.Column(
                        [url_box,
                         ft.Row([url_count_txt,
                                 ft.Container(expand=True),
                                 ft.Text("one link per line", size=11,
                                         font_family="monospace",
                                         color=ft.Colors.ON_SURFACE_VARIANT)],
                                spacing=8)],
                        spacing=6,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    )),
                    # Actions stacked beside the input rather than below it, so
                    # the eye does not have to travel past the textarea to find
                    # the button that uses what was typed into it.
                    #
                    # An explicit width, not expand: the input takes the slack
                    # and the buttons stay one comfortable click-target wide.
                    ft.Container(
                        width=176,
                        content=ft.Column(
                            [fetch_btn,
                             ft.OutlinedButton("Load .txt",
                                               icon=ft.Icons.FILE_OPEN,
                                               on_click=on_load_file,
                                               style=_ink_button_style()),
                             ui.flat_button("Clear", ft.Icons.CLEAR,
                                            on_clear)],
                            spacing=8, tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                        ),
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),

            fetch_row,
            status_row,

            # Output mode: muted label, linked toggle, explanation underneath.
            ft.Column(
                [ui.section_label("Output mode"),
                 ft.Row([mode_toggle, mode_blurb_txt], spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER)],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.START,
            ),

            # Preview. No outer card -- preview_list already renders each row
            # as its own card, so wrapping the list in a fourth box would put a
            # border around a stack of borders.
            ft.Column(
                [ft.Row(
                    [ui.section_label("Preview"), ft.Container(expand=True),
                     count_txt,
                     ft.TextButton("Select all", on_click=on_select_all),
                     ft.TextButton("Select none", on_click=on_select_none)],
                    spacing=8),
                 preview_list,
                 # No wrap=True: the Flutter Wrap cannot hold expand=True
                 # children, and the combination collapsed the layout into an
                 # unbounded (~100k px) scroll gap that pushed the primary
                 # "Download selected" CTA off-screen.
                 ft.Row([selected_txt, dl_btn], spacing=12,
                        alignment=ft.MainAxisAlignment.END),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                # The list is the only part of this view that should grow to
                # fill a tall window. expand on the Column, not on the page.
                expand=True,
            ),
        ], spacing=20, scroll=ft.ScrollMode.AUTO, expand=True,
           horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

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
        # The page comes from _main_page, NOT from queue_list.page.
        #
        # It used to probe the control: `queue_list.page`, and bail when that
        # was None. That probe is wrong in the one place it matters. on_nav()
        # assigns body_content.content = views[idx] and then calls
        # refresh_queue() -- but a Flet tree is not mounted until page.update()
        # runs, so the freshly-selected view is still detached at that instant.
        # Every tab switch to Queue therefore bailed out, and the list stayed
        # empty until something called refresh_queue() again later, which only
        # happened once a download was already running. In practice: open a
        # fresh queue, add nothing, start nothing, and the tab is blank even
        # with rows sitting in the store.
        #
        # Rendering into a detached view is harmless -- the controls are built
        # either way and are serialised the next time the view is mounted.
        page = _main_page
        if page is None:
            # _main has not run yet (build-time construction). Nothing to
            # repaint, but keep the store's on-disk copy current.
            try:
                store.save()
            except Exception:
                pass
            return
        import flet as ft
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
        
        # The empty state is a SIBLING of the ListView (see queue_empty in
        # queue_view), not a child of it. Appending it to queue_list looked
        # right and produced nothing on screen: refresh_queue() runs while
        # queue_view() is still building, so the control is mounted by a
        # page.update() that has no page to update yet. An empty queue showed a
        # bare toolbar over an empty screen. Toggling a sibling's `visible`
        # needs no such thing -- it is already mounted by the time any item
        # arrives.
        queue_empty.visible = not items
        queue_list.visible = bool(items)

        if not items:
            if not _queue_empty_shown:
                queue_list.controls.clear()
                header.value = "Downloads (0)"
                _queue_empty_shown = True
                try:
                    page.update()
                except Exception:
                    pass
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
                    # A bare ft.Text now, so the value is set directly. It used
                    # to reach through .content into the pill's Text.
                    widgets['pct_pill'].value = f"{pct:.1f}%"
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
                # Flat 28px icon buttons, no filled chip behind each one.
                # The old version wrapped every action in its own
                # SURFACE/PRIMARY_CONTAINER/ERROR_CONTAINER square, so a row
                # with a retry and a stop showed two saturated badges sitting
                # above the status text -- louder than the filename they belong
                # to. Reference: button.image-button, flat, ink on hover.
                actions = []
                if it.status in ("error", "cancelled"):
                    actions.append(ui.icon_button(
                        ft.Icons.REFRESH, "Retry",
                        lambda e, _id=it.id: (store.update(_id, status="queued",
                                                          error=""),
                                             start_soon(page))))
                if it.status in ("downloading", "converting", "fetching"):
                    actions.append(ui.icon_button(
                        ft.Icons.STOP_CIRCLE_OUTLINED,
                        "Stop current download (rest stay queued)",
                        lambda e, _id=it.id: cancel_id(page, _id),
                        destructive=True))
                if it.status not in ("downloading", "converting", "fetching"):
                    actions.append(ui.icon_button(
                        ft.Icons.CLOSE, "Remove",
                        lambda e, _id=it.id: (store.remove(_id),
                                             refresh_queue()),
                        destructive=True))
                title_txt = _sanitize_display(f"{it.title}{it.ext}", limit=120)
                # Wraps to two lines rather than ellipsising. no_wrap=True +
                # max_lines=1 + overflow=ELLIPSIS is the obvious way to do this
                # and it does not work here: flet 0.86.5 clips the string at
                # the card edge with no ellipsis and no wrap, so a long title
                # just stopped mid-word. Two lines always shows a complete,
                # predictable amount of the name at any window width.
                title_col: list = [ft.Text(title_txt, size=13,
                                           weight=ft.FontWeight.W_600,
                                           max_lines=2,
                                           overflow=ft.TextOverflow.ELLIPSIS)]
                if subtitle_bits:
                    title_col.append(ft.Text("  \u2022  ".join(subtitle_bits), size=11,
                                             color=ft.Colors.ON_SURFACE_VARIANT))
                
                # Create mutable widgets we'll update later
                pct_pill_widget = pct_pill(f"{pct:.1f}%") if it.status in (
                    "downloading", "converting") else ft.Container()
                
                # Row anatomy follows the reference's .download-row:
                #   title (13px/600)
                #   6px progress, flush under the title
                #   status line ................. [x] [v] [pause]
                # Actions sit on the status line at the right, not stacked at
                # the top-right of the title. They act on the transfer, which is
                # what the status line describes, and putting them there stops
                # the header row from reflowing every time an item changes
                # state.
                head_row = ft.Row([state_icon(it.status),
                                   ft.Column(title_col, spacing=2, expand=True),
                                   pct_pill_widget],
                                  spacing=8,
                                  vertical_alignment=ft.CrossAxisAlignment.CENTER)
                card_body: list = [head_row]

                progress_bar = None
                detail_txt = None
                if it.status in ("downloading", "converting"):
                    speed = getattr(it, "download_speed", 0) or 0
                    speed_txt = f"  \u2022  {format_bytes(speed)}/s" if speed > 0 else ""
                    detail = (f"{format_bytes(it.current_bytes)}/{format_bytes(it.total_bytes or it.size)}"
                              + speed_txt
                              + ("  \u2022  converting\u2026" if it.status == "converting"
                                 else "  \u2022  downloading\u2026"))
                    progress_bar = ui.progress_bar(pct / 100)
                    detail_txt = ft.Text(detail, size=11,
                                         color=ft.Colors.ON_SURFACE_VARIANT,
                                         weight=ft.FontWeight.W_500)
                    # The bar is a button. It already looks like a scrubber, so
                    # clicking it has to do something -- see toggle_pause. The
                    # padding is vertical-only: a 6px bar is a miserable click
                    # target, and the hit area is grown without the card
                    # gaining a visible edge.
                    card_body.append(ft.Container(
                        content=progress_bar,
                        padding=ft.Padding(0, 7, 0, 7),
                        on_click=lambda e, _id=it.id: toggle_pause(page, _id),
                        tooltip="Pause this download (it resumes from where "
                                "it stopped)",
                    ))
                elif it.status in ("queued", "cancelled"):
                    # Plain-language status line for idle states.
                    detail_txt = status_label(it.status)
                elif it.status == "fetching":
                    detail_txt = ft.Text("Looking up file details\u2026", size=11,
                                         color=ft.Colors.ON_SURFACE_VARIANT)
                else:
                    detail_txt = None

                # Outcome first, actions last. A finished row used to render
                # its action icons on a line of their own, then the destination
                # or error text BELOW them -- so the controls floated in the
                # middle of the card, above the thing they refer to. Actions
                # belong on the final line, bottom-right.
                if it.status == "error" and it.error:
                    card_body.append(_row_note(
                        ft.Icons.ERROR_OUTLINE, ft.Colors.ERROR,
                        _sanitize_display(it.error)))
                if it.status == "done":
                    if it.dest:
                        card_body.append(_row_note(
                            ft.Icons.CHECK_CIRCLE_OUTLINE, ft.Colors.TERTIARY,
                            it.dest, mono=True))
                    elif it.error:
                        # Kept-original note (e.g. ffmpeg missing) surfaces here.
                        card_body.append(hint_box(it.error))

                # Status line: what is happening on the left, what you can do
                # about it on the right.
                if detail_txt is not None or actions:
                    card_body.append(ft.Row(
                        [detail_txt if detail_txt is not None
                         else ft.Container(expand=True),
                         *(actions if detail_txt is not None
                           else [ft.Container(expand=True), *actions])],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ))
                
                # The one place a card is correct: this is a list item. Same
                # 12px radius / 1px hairline / 12px pad as every other card.
                card = ui.card(ft.Column(card_body, spacing=8))
                new_controls.append(card)
                
                # Store references to mutable widgets
                _queue_widgets[it.id] = {
                    'card': card,
                    'progress_bar': progress_bar,
                    # Only cache a REAL pill. Two guards have been wrong here,
                    # and both killed the queue view (and with it the app) by
                    # making the next refresh do `None.value = ...`:
                    #   - `hasattr(w, "content")` is True for every ft.Container,
                    #     because `content` is a dataclass field.
                    #   - `getattr(w, "content", None) is not None` is False for
                    #     the current ft.Text, which has no `content` at all.
                    # Test for the type the update path actually writes to.
                    'pct_pill': (pct_pill_widget
                                 if isinstance(pct_pill_widget, ft.Text)
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

    def toggle_pause(page, target_id):
        """Pause an active download, or resume a paused one.

        Users read the progress bar as a control -- it looks exactly like the
        scrubber on a media player, so they drag and click it expecting it to
        respond. ft.ProgressBar is display-only, so every one of those clicks
        did nothing at all, which read as a broken app rather than a read-only
        indicator. Clicking it now pauses; clicking again resumes.

        Pause goes through cancel_id, which sets the run's cancel event. That
        looks drastic but it is the only stop primitive the engine has, and
        download_resumable() keeps the <name>.part plus its sidecar, so the
        next run continues from the byte offset instead of starting over. Only
        one batch runs at a time, so pausing one item does pause the batch.
        """
        it = store.get(target_id)
        if not it:
            return
        if it.status in ("downloading", "converting", "fetching"):
            cancel_id(page, target_id)
            return
        if it.status == "cancelled":
            # Re-queue and let start_queue's re-drain pick it up, clearing the
            # error so the row does not still read "cancelled".
            store.update(target_id, status="queued", error="")
            refresh_queue()
            snack(page, "Resuming — continues from where it stopped.")
            start_soon(page)
            return
        snack(page, "Only a running or paused download can be paused.",
              error=True)

    # Bound before queue_view/refresh_queue can run: refresh_queue() reads and
    # writes it, and it is called from more than one place.
    _queue_empty_shown = False
    queue_empty = ft.Container()
    # The live ft.Page, captured in _main. refresh_queue() and
    # refresh_settings_hint() need a page to repaint and cannot get one from a
    # control, because the view holding that control may be detached.
    _main_page = None

    def queue_view(page: ft.Page):
        import flet as ft
        nonlocal queue_list, _queue_empty_shown, queue_empty
        _queue_empty_shown = False
        queue_list = ft.ListView(spacing=10, expand=True)
        queue_empty = empty_state(
            ft.Icons.DOWNLOAD_DONE, "No downloads in queue",
            "Add links from the Add tab to get started.",
            action_label="Add downloads", action_icon=ft.Icons.ADD,
            on_action=lambda e: nav["goto"](0) if "goto" in nav else None)
        header = ft.Text(f"Downloads ({len(store.items)})", size=14,
                         weight=ft.FontWeight.BOLD)
        summary = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
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

        # Bare toolbar on the scaffold. This was a SURFACE_CONTAINER slab, so
        # the page opened with a 18px-padded box containing a title, a summary
        # and two buttons -- and then a stack of cards below it. The queue
        # itself is the content; the controls are chrome.
        toolbar = ft.Row([
            ft.Icon(ft.Icons.DOWNLOADING, size=14, color=ft.Colors.PRIMARY),
            header,
            ft.Container(expand=True),
            summary,
            ft.TextButton("Clear finished", icon=ft.Icons.DELETE_SWEEP,
                          on_click=lambda e: (store.clear_finished(),
                                              refresh_queue())),
            ui.flat_button("Resume all / Start", ft.Icons.PLAY_ARROW,
                           on_resume_all),
            ui.flat_button("Cancel batch", ft.Icons.STOP_CIRCLE_OUTLINED,
                           on_cancel, destructive=True),
        ], spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER)
        refresh_queue()
        return ft.Column([
            h1(ft.Icons.QUEUE, "Queue",
               "Track progress, retry errors, clear finished downloads."),
            toolbar,
            queue_empty,
            queue_list,
        ], spacing=16, expand=True,
           # STRETCH so every queue card is exactly the width of the widest one.
           # Without it the ListView hands each child its intrinsic width, cards
           # come out ragged, and the scrollbar tracks the tallest rather than
           # the widest.
           horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    # ------------------------------------------------------------ settings
    settings_refs: dict = {}

    def refresh_settings_hint():
        try:
            st = load_settings()
            if settings_refs.get("mode_txt") is not None:
                settings_refs["mode_txt"].value = MODE_LABELS.get(st.output_mode, st.output_mode)
            # The blurb has to track the mode too: it is chosen on the Add tab,
            # so a stale explanation next to a fresh label is worse than none.
            if settings_refs.get("mode_blurb") is not None:
                settings_refs["mode_blurb"].value = MODE_BLURB.get(
                    st.output_mode, MODE_BLURB["1"])
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
                       text_size=13)
        # Built per-view, not at import: ft.Colors.* resolves against the
        # active theme at access time, so a module-level constant would freeze
        # whichever theme happened to be active on first import.
        _label_style = ft.TextStyle(size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        # No `label=` on these fields: labelled() renders the caption above the
        # box so it can share that row with a right-aligned hint. Leaving the
        # Flet label in place as well printed the field name twice.
        api_id_f = ft.TextField(value=st.api_id or "", **_fld_kw)
        api_hash_f = ft.TextField(value=st.api_hash or "",
                                  password=True, can_reveal_password=True,
                                  **_fld_kw)
        # No expand=True: the parent is the scrollable settings Column, where an
        # expanded TextField lays out at zero size and renders nothing. The
        # surrounding group() already stretches it via STRETCH.
        dl_f = ft.TextField(label="Download folder", label_style=_label_style,
                            value=st.download_dir or str(default_download_dir()),
                            **_fld_kw)
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
        mode_txt = ft.Text(MODE_LABELS.get(st.output_mode, st.output_mode),
                           size=13, weight=ft.FontWeight.W_600)
        folder_txt = ft.Text(st.download_dir or str(default_download_dir()), size=13,
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

        async def on_check_updates(e):
            # No button bookkeeping here any more. This handler used to poke
            # check_updates_btn.disabled/text/icon by hand, which was the old
            # way of signalling "busy" -- it left the button live-looking for
            # the whole network round-trip. pending_button() now owns that state
            # (disabled + spinner for the duration of the await), so the handler
            # only has to do the work.
            try:
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
            except Exception as exc:
                snack(page, f"Update check failed: {_sanitize_display(str(exc))}",
                      error=True)

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
            # No re-entrancy guard needed here. This used to disable
            # check_updates_btn for the duration of the download so a second
            # click could not start a second install. pending_button() already
            # holds the button disabled for the whole await chain, and
            # on_check_updates awaits this function -- so the guard was both
            # redundant and, once that button was removed from the tree, a
            # NameError waiting to happen.
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
            # Destructive and irreversible: clears the saved Telegram session,
            # the credentials and the whole queue. A plain yes/no sat one stray
            # click away from wiping a logged-in session, so this now requires
            # typing the word.
            cp = await typed_confirm_dialog(
                page,
                "Reset everything?",
                "This signs you out of Telegram, deletes your saved API "
                "credentials, and empties the download queue. Files already "
                "downloaded are not touched. This cannot be undone.",
                phrase="RESET", action="Reset everything",
                icon=ft.Icons.WARNING_AMBER)
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
                # Drive the visible toggle, not a hidden control. The old
                # SegmentedButton this replaced was visible=False and read by
                # nothing else, so a reset left the painted mode cards showing
                # the old mode until the view happened to be rebuilt.
                ms = state.get("mode_set")
                if ms is not None:
                    ms("1")
            except Exception:
                pass
            refresh_queue()
            refresh_settings_hint()
            snack(page, "Reset complete. Re-enter API credentials.")

        # ── Layout ────────────────────────────────────────────────────
        first = not st.api_id or not st.api_hash or not st.download_dir

        # What each output mode does lives in MODE_BLURB (module level, so the
        # shared refresh_settings_hint can keep it in step with the Add tab).
        _mk = st.output_mode if st.output_mode in MODE_BLURB else "1"

        # --- Actions ---------------------------------------------------
        # These act on the credentials above, so they sit inside that group.
        # Floating between groups made them look unrelated to anything.
        action_buttons = ft.Row([
            pending_button("Save", ft.Icons.SAVE, on_save, kind="filled"),
            pending_button("Verify / Login", ft.Icons.LOGIN, on_login,
                           kind="tonal"),
            pending_button("Log out", ft.Icons.LOGOUT, on_logout,
                           kind="outline"),
        ], spacing=10, wrap=True)

        # --- Account ---------------------------------------------------
        # The two credential fields sit SIDE BY SIDE and share the full width,
        # rather than stacking as two half-filled rows. Stacking is what forced
        # the FIELD_MAX cap in the first place -- one 520px field on a 1900px
        # window left half the row empty and read as a broken layout. Side by
        # side, each field is still a sane measure and the row is full. Same
        # shape the reference uses for "Default video format & quality":
        #     [MP4 v]        [2160p (4K) v]
        api_card_body = []
        if first:
            # Bare caption, not a box. It is advice, not data, and the reference
            # gives its equivalent notes no border either -- a note with its own
            # frame outranks the fields it is explaining.
            api_card_body.append(ui.caption(
                "Create these at my.telegram.org → API development tools. "
                "They are stored privately (0600) and only asked once."))
        # The field labels live outside the border so each can carry a
        # right-aligned hint saying where the value comes from, which is the
        # single most common place new users get stuck.
        api_card_body += [
            ft.Row(
                [
                    labelled("API ID", api_id_f,
                             "my.telegram.org → App api_id", expand=True),
                    labelled("API hash", api_hash_f, "128-bit hex string",
                             expand=True),
                ],
                spacing=12,
                # STRETCH so each labelled() actually fills its half. Without it
                # the Row hands each child its intrinsic width and both fields
                # shrink back to a fraction of the space.
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            action_buttons,
        ]

        api_card = group("Telegram account", api_card_body,
                         icon=ft.Icons.KEY,
                         subtitle="Required once. Stored on this machine only.",
                         action=badge("UNSET" if first else "STORED", "accent"))

        # --- Output mode ------------------------------------------------
        # Both the label and the explanation are live controls held in
        # settings_refs: the mode is chosen on the Add tab, so this group has to
        # follow it rather than freezing whatever was set at build time.
        mode_blurb_txt = ft.Text(MODE_BLURB[_mk], size=12.5,
                                 color=ft.Colors.ON_SURFACE_VARIANT)
        settings_refs["mode_blurb"] = mode_blurb_txt
        mode_card = group(
            "Output mode",
            [
                ft.Text(MODE_BLURB[_mk], size=12.5,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text("If a video is already H.264/AAC it is repackaged "
                        "without re-encoding, so all three behave the same. "
                        "Change it per download on the Add tab.",
                        size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            icon=ft.Icons.TUNE,
            action=ft.Row([ft.Text(MODE_LABELS.get(_mk, _mk), size=12,
                                   weight=ft.FontWeight.W_600,
                                   color=ft.Colors.PRIMARY),
                           badge("DEFAULT", "neutral")], spacing=8))

        # --- About ------------------------------------------------------
        # One of the two groups that keeps a card. It holds a table of
        # name/value rows, and a table is exactly what a border is for -- same
        # as the Dependencies list in the reference.
        about_card = group(
            "About",
            [
                *_about_rows(),
            ],
            icon=ft.Icons.INFO,
            subtitle="Version, engine and updater.",
            card=True,
            action=pending_button("Check for updates", ft.Icons.UPDATE,
                                  on_check_updates, kind="outline"))

        # --- Danger zone -------------------------------------------------
        # Flat destructive row, not a red box. The reference has no danger zone
        # at all, but the principle it follows applies: the button carries the
        # colour, and a full-width tinted panel around one action gives that
        # action more visual weight than the account fields above it.
        danger_zone = ft.Row(
            [
                ft.Icon(ft.Icons.WARNING_AMBER, size=14,
                        color=ft.Colors.ERROR),
                # Action sits on the same row as the title, so the warning and
                # the thing that triggers it are read together instead of being
                # separated by a paragraph.
                ft.Column([
                    ft.Text("Reset everything", size=12.5,
                            weight=ft.FontWeight.W_600),
                    ft.Text("Signs you out of Telegram, deletes your API "
                            "credentials and empties the queue. You will be "
                            "asked to type RESET to confirm.",
                            size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=2, expand=True),
                ui.flat_button("Reset", ft.Icons.DELETE_FOREVER,
                               on_click=on_reset, destructive=True),
            ],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # Single column. A two-column variant was tried and reverted: putting
        # deps_view (a Column with expand=True) inside an unbounded Row/Column
        # gives it no definite height, and the whole view collapsed to nothing.
        # Going wide properly needs the deps view to stop expanding, which is a
        # larger change than it looks.
        return ft.Column([
            h1(ft.Icons.SETTINGS, "Settings",
               "Your Telegram account, output format and updates."),
            api_card,
            settings_error,
            mode_card,
            deps_section(page),
            about_card,
            danger_zone,
        ], spacing=26, scroll=ft.ScrollMode.AUTO, expand=True,
           horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    # ------------------------------------------------------------ deps page
    def deps_view(page: ft.Page, header: bool = True):
        import flet as ft
        col = ft.Column(spacing=20, scroll=ft.ScrollMode.AUTO, expand=True,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        sys_txt = ft.Text(f"{platform.system()} {platform.release()} • Python {platform.python_version()}",
                          size=11, color=ft.Colors.ON_SURFACE_VARIANT)

        def render(rows_data=None):
            col.controls.clear()
            # header=False when embedded as a section inside Settings, which
            # already has its own page title and scroll container.
            if header:
                col.controls.append(h1(ft.Icons.BUILD, "Dependencies",
                                       "Engine status for download + convert."))
                # Only on the standalone page. Emitted unconditionally it landed
                # between two Settings cards with no heading, reading as a stray
                # line of text rather than part of anything. The platform and
                # Python version are both in the About card already.
                col.controls.append(sys_txt)
            if rows_data is None:
                # First build happens before the view is shown, so the sync
                # subprocess probes here are acceptable. Repeated checks go
                # through _recheck (thread) to keep the UI responsive.
                rows_data = deps_mod.check_all()
            rows: list = []
            for d in rows_data:
                # One row per dependency: icon, name, hint underneath, version
                # pushed right in mono, and a status chip. The generated design
                # scans because the version column is right-aligned in
                # monospace, so every value lines up regardless of length.
                row = ft.Container(
                    ft.Row([
                        ft.Icon(ft.Icons.CHECK_CIRCLE if d.ok
                                else ft.Icons.ERROR_OUTLINE,
                                size=14,
                                color=STATUS_COLORS["done"] if d.ok
                                else STATUS_COLORS["error"]),
                        ft.Column([
                            ft.Text(d.name, size=12, weight=ft.FontWeight.W_500),
                            ft.Text(d.hint or "", size=11,
                                    color=ft.Colors.ON_SURFACE_VARIANT),
                        ], spacing=1, expand=True),
                        mono(d.version or "not detected", size=11,
                             color=ft.Colors.ON_SURFACE_VARIANT),
                        badge("OK" if d.ok else "MISSING",
                              "ok" if d.ok else "bad"),
                    ], spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding(10, 8, 10, 8),
                    border_radius=6)
                rows.append(row)
                if not d.ok:
                    # The command is the whole point of a MISSING row, so it
                    # gets its own line with a $ prompt and a Copy button --
                    # users retype these and get them subtly wrong otherwise.
                    rows.append(ft.Row([
                        ft.Text("$", size=11, font_family="monospace",
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        mono(install_cmd_for(d), size=11,
                             color=ft.Colors.ON_SURFACE_VARIANT),
                        ft.Container(expand=True),
                        ft.TextButton(
                            "Copy",
                            icon=ft.Icons.CONTENT_COPY_OUTLINED,
                            on_click=lambda e, _c=install_cmd_for(d):
                                _copy_clipboard(page, _c),
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(size=11))),
                    ], spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER))
            async def _recheck(e):
                # check_all spawns subprocesses (ffprobe -version, etc.):
                # offload to a thread so repeated checks never freeze the UI.
                # No manual disabled/"Checking…" bookkeeping here -- the button
                # is built by pending_button, which owns that state, exactly as
                # it does for Save, Verify and Check for updates.
                try:
                    data = await asyncio.to_thread(deps_mod.check_all)
                    render(data)
                except Exception as exc:
                    snack(page, f"Dependency check failed: {exc}", error=True)

            rows.append(ft.Row([pending_button("Re-check", ft.Icons.REFRESH,
                                              _recheck)],
                               alignment=ft.MainAxisAlignment.START))
            col.controls.append(group(
                "Dependencies", rows, icon=ft.Icons.CHECKLIST,
                subtitle="Required tools for download and convert.",
                card=True))
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

    async def typed_confirm_dialog(page, title: str, body: str,
                                   phrase: str, action: str,
                                   icon=ft.Icons.WARNING_AMBER) -> bool:
        """Destructive confirmation the user must complete by typing *phrase*.

        A plain "are you sure?" is one stray click away from deleting a saved
        Telegram session and the whole queue, and the reset button sits right
        under ordinary settings controls where a mis-click is likely. Typing a
        word forces a deliberate act.

        The confirm button stays disabled until the text matches exactly, so it
        cannot be dismissed by reflex.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = ft.TextField(hint_text=phrase, autofocus=True,
                             border_color=ft.Colors.OUTLINE_VARIANT,
                             border_radius=8, dense=True)
        ok = ft.FilledButton(action, color=ft.Colors.ERROR,
                             disabled=True)

        def _close(result):
            if not fut.done():
                fut.set_result(result)
            _hide_dlg(page, dlg)

        def _on_type(e):
            ok.disabled = (e.control.value or "").strip() != phrase
            try:
                ok.update()
            except Exception:
                pass

        entry.on_change = _on_type
        ok.on_click = lambda e: _close(True)

        dlg = ft.AlertDialog(
            title=ft.Text(title), icon=ft.Icon(icon),
            content=ft.Column([
                ft.Text(body, size=12),
                ft.Text(f"Type {phrase} to confirm:", size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                entry,
            ], spacing=10, tight=True),
            actions=[ft.TextButton("Cancel", on_click=lambda e: _close(False)),
                     ok],
        )
        _show_dlg(page, dlg)
        return bool(await fut)

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
        nonlocal _main_page
        _main_page = page
        page.title = "Telegram Downloader"
        # Theme comes from ui.theme(). The navy/Telegram token set lives in
        # ui.py, which is the single source of truth for colour; this file used
        # to carry its own PALETTES dict plus a hand-built ColorScheme, which is
        # how the amber and blue-slate schemes drifted out of step with each
        # other. The scrollbar theme is set inside ui.theme() too.
        dark_theme = ui.theme(dark=True)
        light_theme = ui.theme(dark=False)

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
            icon_size=18,
            icon_color=ft.Colors.ON_SURFACE_VARIANT,
        )
        # Sync initial icon with current theme
        _st0_icon = load_settings()
        if (_st0_icon.theme or "dark") == "light":
            theme_btn.icon = ft.Icons.DARK_MODE
            theme_btn.tooltip = "Switch to dark mode"

        # -- Brand mark ------------------------------------------------------
        # The APP's logo, not the Telegram paper plane. assets/icon.svg ships
        # inside the package, so the title bar shows the real artwork in every
        # install. Loaded as a self-contained data URI (see app_logo_src)
        # because ft.app() here accepts no assets_dir to serve the file from.
        # If the asset cannot be read, fall back to a drawn square + download
        # arrow -- a broken image box is worse than a close copy.
        #
        # The bar itself is built further down, after the nav chips exist, so
        # that brand + tabs + toggle all end up in ONE row instead of the two
        # stacked full-width bars this replaced.
        logo_src = app_logo_src()
        if logo_src is not None:
            app_logo = ft.Image(
                src=logo_src, width=20, height=20,
                fit=ft.BoxFit.CONTAIN,
            )
        else:
            app_logo = ft.Container(
                width=20, height=20,
                border_radius=0,
                bgcolor="#2AABEE",
                alignment=ft.Alignment.CENTER,
                padding=0,
                content=ft.Icon(ft.Icons.DOWNLOAD, color="#0F1117", size=13),
            )

        # -- Body container ------------------------------------------------
        # The scroll container owns scrolling; each view is a plain Column
        # inside it. Previously every view was its own scrollable Column nested
        # in a capped box, which is how content ended up a narrow slab in the
        # middle of the window with the nav pinned far to the left.
        #
        # No width cap: the content fills the window and only the gutter
        # constrains it, so the screen is actually used. `expand` on BOTH
        # containers matters -- with it only on the outer one the inner box
        # sized itself to its content and overflowed instead of scrolling, so
        # everything below the fold was unreachable.
        body_content = ft.Container(expand=True)
        body = ft.Container(
            content=ft.Container(
                content=body_content,
                expand=True,
                padding=ft.Padding(SHELL_PAD, 16, SHELL_PAD, 16),
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
            """A tab with a 3px accent underline, not a filled pill.

            From simple-yt-downloader's style.py:
                stackswitcher > button { border-bottom: 3px solid transparent }
                stackswitcher > button:checked { border-bottom: 3px solid @accent;
                                                 color: @accent; font-weight: 600 }

            The filled-chip version made three saturated blocks sit in the top
            bar competing with the content; an underline is a 3px mark that
            says "you are here" without adding a box.
            """
            def _pick(e):
                _goto(idx)
            tab = ft.Container(
                content=ft.Text(label, size=13, weight=ft.FontWeight.W_600,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                padding=ft.Padding(10, 8, 10, 8),
                # bottom edge only; transparent when unselected so nothing shows
                border=ft.Border.only(bottom=ft.BorderSide(3,
                                                           ft.Colors.TRANSPARENT)),
                on_click=_pick,
                # Identity is carried on the tab, not implied by its position in
                # _tab_chips. queue_chip is built FIRST (refresh_queue needs a
                # handle on it) but renders SECOND, so enumerate() order is
                # [Queue, Add, Settings] while the screen shows
                # [Add, Queue, Settings] -- clicking Add lit up Queue.
                data=idx,
            )
            _tab_chips.append(tab)
            return tab

        queue_chip = _make_chip(1, "Queue", ft.Icons.QUEUE_OUTLINED)
        _add_chip = _make_chip(0, "Add", ft.Icons.LINK_OUTLINED)
        _settings_chip = _make_chip(2, "Settings", ft.Icons.SETTINGS_OUTLINED)

        # ONE top bar: brand, underlined tabs, theme toggle.
        #
        # This replaces a full-bleed #2AABEE AppBar stacked above a second
        # full-bleed tab strip. Two bars, a saturated accent used as a
        # background, and a third alignment axis for the centred body below --
        # that stack is why the app read as a government form. Brand, tabs and
        # toggle belong on one line; colour now means status, not chrome.
        top_bar = _bar(
            ft.Row(
                [
                    app_logo,
                    ft.Text("Telegram Downloader", size=13,
                            weight=ft.FontWeight.W_600),
                    ft.Container(width=16),   # gap between brand and tabs
                    _add_chip, queue_chip, _settings_chip,
                    ft.Container(expand=True),   # pushes toggle to the right
                    theme_btn,
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=ft.Colors.SURFACE,
            bottom_border=True,
            pad_y=4,
        )

        # Expose the Queue chip so refresh_queue can put the pending count on it.
        queue_header_txt["tab"] = queue_chip
        # Which view is on screen. Guards re-taps of the active chip.
        _current_idx = -1

        def _set_selected_index(idx: int):
            # Selection is an accent underline plus accent label text. Matched on
            # chip.data, never enumerate order -- see _make_chip.
            #
            # No animation on purpose: this is a 3px border and a colour change
            # on three small controls. The old version animated opacity, which
            # faded the WHOLE tab so an unselected one ghosted at partial alpha
            # instead of going cleanly flat.
            for chip in _tab_chips:
                on = (chip.data == idx)
                chip.border = ft.Border.only(
                    bottom=ft.BorderSide(3, ft.Colors.PRIMARY if on
                                         else ft.Colors.TRANSPARENT))
                chip.content.color = (ft.Colors.PRIMARY if on
                                      else ft.Colors.ON_SURFACE_VARIANT)

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
        dl_path_txt = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT,
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

        # Download location, pinned to the bottom and editable from any tab.
        #
        # A slim CARD, not a full-width bar. From the reference:
        #     .download-row.path-bar { padding: 8px 10px; }
        # It is the one non-list item that earns a card, because it is a
        # persistent object the user acts on rather than a section heading.
        # Previously this ran edge-to-edge as a bar while the body was inset,
        # which put the folder path hard against the window and Change hard
        # against the other side.
        dl_bar = _bar(
            ui.card(
                ft.Row(
                    [
                        ft.Icon(ft.Icons.FOLDER_OUTLINED, size=14,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        dl_path_txt,
                        ft.Container(expand=True),
                        # Reveal the folder in the desktop file manager. This
                        # lived in a "Quick Actions" card on the Add tab, which
                        # gave two buttons -- one of them duplicating the tab
                        # strip -- a full bordered panel. Next to the path they
                        # actually refer to, it costs one 28px button.
                        ui.icon_button(ft.Icons.OPEN_IN_NEW,
                                       "Open download folder",
                                       lambda e: open_folder(page)),
                        ft.Container(
                            content=ft.Text("Change", size=12,
                                            color=ft.Colors.ON_SURFACE),
                            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                            border_radius=7,
                            padding=ft.Padding(12, 6, 12, 6),
                            on_click=_pick_download_dir,
                            ink=True,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                pad=8,
            ),
            bgcolor=ft.Colors.SURFACE,
            top_border=True,
            pad_y=8,
        )
        _refresh_dl_label()

        # -- Page shell: top bar, body, footer ------------------------------
        # No page.appbar any more: the brand, the tabs and the toggle are all
        # rows inside top_bar, so there is one header instead of two.

        # First-run nudge: open Settings when API/folder missing.
        st0 = load_settings()
        start_idx = 2 if (not st0.api_id or not st0.api_hash or not st0.download_dir) else 0
        _set_selected_index(start_idx)
        body_content.content = views[start_idx]
        _apply_layout()

        page.add(ft.Column(
            [
                top_bar,
                body,
                dl_bar,
            ],
            expand=True,
            spacing=0,
        ))

        # -- First run: required-dependency prompt ------------------------
        # The app used to launch straight into a half-working state and only
        # reveal a missing dependency once a download failed with some
        # unrelated-looking error. A missing REQUIRED package is a hard stop,
        # so say so at launch, by name, with the command to fix it.
        async def _first_run_dep_check():
            try:
                missing = await asyncio.to_thread(deps_mod.missing_required)
            except Exception:
                return          # never let the check itself break startup
            if not missing:
                return
            names = "\n".join(f"  •  {d.name}" for d in missing)
            cmds = "\n".join({install_cmd_for(d) for d in missing} - {""})
            body = (f"This app cannot download until these are installed:\n\n"
                    f"{names}\n\n"
                    f"Install with:\n{cmds}\n\n"
                    "Settings → Status has the same list and a Re-check button.")
            go = await confirm_dialog(
                page, "Missing dependencies", body,
                action="Open Settings", icon=ft.Icons.WARNING_AMBER)
            if go:
                _goto(2)

        page.run_task(_first_run_dep_check)

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
