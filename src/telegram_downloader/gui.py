"""The Flet GUI: Add / Queue / Settings on top of the ui design system.

Three ideas hold this together, and every one of them is a correction of
something that shipped broken:

1. ONE ACCENT, AND IT MEANS ACTION. Colour lives in ui.py and nowhere else.
   This file used to carry its own token dicts and hand-built palettes, which
   is how the chrome and the buttons drifted apart. There is no colour here
   that ui.py does not define.

2. THE TAB STRIP IS THE PAGE TITLE. There is no per-page heading. Add / Queue
   / Settings sit in the top bar as chips, the selected one is a raised pill,
   and the content below starts immediately.

3. EVERY LONG ACTION SHOWS IT IS WORKING. pending_button() swaps in a spinner
   for the duration of its await, so Save, Verify, Log out, Re-check and Check
   for updates can never look frozen.

Chrome mirrors the previous layout deliberately: navigation across the TOP,
the download folder pinned to the BOTTOM and changeable from any tab, and
Dependencies as a Settings section rather than its own tab.

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

import re as _re_module

from . import converter, deps as deps_mod
from . import tg_client
from . import ui
from . import updates
from .config import (_sanitize_display, Settings, clear_session,
                     ensure_download_dir,
                     harden_session_files, load_settings,
                     save_settings)
from .constants import MAX_BATCH_BYTES
from .downloader import Cancelled, download_resumable
from .queue_store import DownloadItem, QueueStore
from .telegram_utils import format_bytes, safe_filename, split_urls

_queue_widgets: dict = {}  # item_id -> {card, progress_bar, detail_txt, sig}

# What each mode actually does, in words. The old Settings card showed only
# "Default: Original", which told the user nothing.
MODE_BLURB = {
    "1": "Keep the file exactly as Telegram sent it. No conversion, so "
         "nothing is ever re-encoded.",
    "2": "Convert to MP4 with a fast encoder preset. Smaller file, quicker to "
         "process.",
    "3": "Convert to MP4 with a better compression preset. Smallest file, "
         "slower to process.",
}

# The store speaks its own seven states; ui.py owns the six-state vocabulary
# every mark and colour is keyed on. This is the only translation point.
_STORE_TO_UI = {
    "queued": ui.STATUS_WAITING,
    "fetching": ui.STATUS_WORKING,
    "downloading": ui.STATUS_WORKING,
    "converting": ui.STATUS_WORKING,
    "done": ui.STATUS_DONE,
    "error": ui.STATUS_FAILED,
    "cancelled": ui.STATUS_CANCELLED,
}

# The idle-state line for a row. Plain sentences, not a coloured chip: the
# row's leading dot already carries the state colour.
_IDLE_LABEL = {
    "queued": "Queued — waiting to start",
    "cancelled": "Cancelled — partial kept for resume",
}


def _row_sig(it) -> tuple:
    """Everything a queue row DISPLAYS, as a comparable key.

    An in-flight row is reused and its progress bar updated in place; any
    change to what the row actually reads (title, extension, error, saved
    path, state word) changes this signature so the row is rebuilt instead.
    """
    return (it.status, it.title, it.ext, it.error, it.dest)


def _progress_detail(it) -> str:
    """The line under a live row's bar: bytes, speed and phase.

    `fetching` has no bytes yet, so it gets the honest sentence instead of a
    meaningless 0 B / 0 B readout.
    """
    if it.status == "fetching":
        return "Looking up file details…"
    speed = getattr(it, "download_speed", 0) or 0
    speed_txt = f"  \u2022  {format_bytes(speed)}/s" if speed > 0 else ""
    phase = "converting…" if it.status == "converting" else "downloading…"
    return (f"{format_bytes(it.current_bytes)}/"
            f"{format_bytes(it.total_bytes or it.size)}"
            + speed_txt + f"  \u2022  {phase}")


def _page_service(page, cls):
    """Return a page-level flet *Service*, created once per page.

    In flet 0.86 `ft.FilePicker` (and friends) derive from `Service`, not from
    the visual `Control` tree. `Service.init()` self-registers under
    `Page._services`, so it must be mounted on the page -- putting it in
    `page.overlay` also tells the Flutter client to render it as a visual,
    which produces a full-height `Unknown control: FilePicker` error stripe
    and a picker that never opens. Caching per page avoids re-registering on
    every click.
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
    pyproject.toml), so the title bar shows the same mark in every install --
    editable, wheel, or the .deb's Python environment. A data URI is
    self-contained: it needs no assets_dir (which ft.app() does not accept
    here) and renders in both the desktop and web views. Returns None on any
    failure so the caller can fall back to a drawn mark instead of a broken
    image box.
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

    store = QueueStore()
    state = {
        "downloading": False,
        "queuing": False,
        "client": None,
        "cancel": None,   # asyncio.Event for the current batch, None between
    }
    nav: dict = {}  # {"goto": fn} once the views exist, so views can switch
    queue_toolbar_summary_ref = None  # Set by build_queue_view(), used by _apply_responsive
    # Page bodies that have two arrangements of the same actions -- a row of
    # buttons for a desktop, a column for a phone -- register both here and
    # the shell shows whichever one fits. See _apply_responsive.
    layout_refs: dict = {}

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
            state["client"] = tg_client.build_client(st.api_id_int(),
                                                     str(st.api_hash))
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
                pinned = ((st.user_id or "").strip()
                          if getattr(st, "user_id", None) else "")
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
                    # That is exactly the attack this pin exists to stop.
                    # The way out is the explicit "Log out" action in
                    # Settings, which clears user_id, never an automatic
                    # side effect of refusing.
                    raise RuntimeError(
                        "A different Telegram account is signed in than the one "
                        "this install is pinned to. For safety you have been "
                        "signed out. If that is expected, use 'Log out' in "
                        "Settings and sign in again; otherwise this session "
                        "file was replaced by someone else.")
                if me_id:
                    state["me_id"] = me_id
            except RuntimeError:
                raise
            except Exception:
                pass
        return c

    # page id -> SnackBar (flet 0.86 has no page.snack_bar)
    _snackbars: dict = {}

    def snack(page, msg: str, error: bool = False):
        # Surface fill and shape come from ui.theme()'s snackbar_theme; only
        # the text colour distinguishes a failure from a confirmation.
        try:
            sb = _snackbars.get(id(page))
            if sb is None:
                sb = ft.SnackBar(ui.text(""))
                _snackbars[id(page)] = sb
                page.overlay.append(sb)
            sb.content = ui.text(msg, color=(ft.Colors.ERROR if error
                                             else ft.Colors.TERTIARY))
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
        """Copy text to the clipboard, falling back to a selection the user
        can copy by hand.

        Flet's clipboard service is a no-op on some desktop builds and the web
        view, and silently doing nothing after the user clicked "Copy" is
        worse than useless. If the service is unavailable the text is shown
        read-only in a dialog so Ctrl+C still works.
        """
        async def _do():
            try:
                svc = _page_service(page, ft.Clipboard)
                await svc.set_clipboard(text)
                snack(page, "Command copied.")
                return
            except Exception:
                pass
            field = ui.text_field(value=text, multiline=True)
            field.read_only = True
            dlg = ft.AlertDialog(
                title=ui.text("Copy this command", role=ui.ROLE_TITLE),
                content=field,
                actions=[ui.action("Close", kind="secondary",
                                   on_click=lambda e: _hide_dlg(page, dlg))])
            _show_dlg(page, dlg)
        page.run_task(_do)

    def install_cmd_for(dep) -> str:
        return dep.install_for_current_os()

    def _grow(control, flex: int = 1):
        """Mark a Row child as taking the leftover width.

        A Row child that neither expands nor sets an explicit width is laid
        out at ZERO width in flet 0.86.5 while the Row still reserves its
        full height, so the failure is a silent blank band rather than a
        missing control -- the Add tab's input row did exactly this. Every
        Row child that has to share the leftover space goes through here.
        """
        control.expand = flex
        return control

    def pending_button(label, icon, handler, kind="primary"):
        """A button that shows it is working while its async handler runs.

        Every long action (verify credentials, log in, check for updates,
        re-check dependencies) used to stay live-looking for the whole network
        round-trip, so people clicked again. This disables the button, swaps
        its content for a spinner, awaits the handler, and restores it.
        """
        busy: dict = {}

        async def _run(e):
            if busy.get("running"):
                return
            busy["running"] = True
            resting = btn.content
            btn.opacity = 0.45
            btn.content = ui.cluster(
                [ui.spinner(size=14),
                 ui.text("Working\u2026", role=ui.ROLE_STRONG)],
                spacing=ui.S2 - 1)
            try:
                btn.update()
            except Exception:
                pass
            try:
                await handler(e)
            finally:
                busy["running"] = False
                btn.opacity = 1
                btn.content = resting
                try:
                    btn.update()
                except Exception:
                    pass

        btn = ui.action(label, icon=icon, kind=kind, on_click=_run)
        return btn

    def _heading(value, trailing=None):
        """A ui.section_label() heading with its None placeholder removed.

        ui.section_label() appends a literal `None` to its row whenever the
        heading has no trailing hint, and the flet client answers a None child
        with a grey error box that swallows every section below it. A heading
        is the only primitive in the design system that can produce one (every
        other optional child is appended conditionally), so the placeholder is
        dropped here; the ui.py row is otherwise untouched.

        Every heading in the app goes through this. Calling ui.section_label()
        directly is the bug this exists to prevent.
        """
        label = ui.section_label(value, trailing=trailing)
        label.controls = [c for c in label.controls if c is not None]
        return label

    # ------------------------------------------------------- output mode
    # The output mode (Original / Fast / Max) is chosen on the ADD page: it
    # is a property of what happens to the links the user is adding, so it
    # is picked in the same breath as the links rather than buried in
    # Settings. The control is built in downloads_view; these are the
    # SHARED HANDLES, because the shell repaints the control when the user
    # navigates back to Add and the control's own builder is a sibling it
    # cannot see.
    mode_refs: dict = {}

    def refresh_mode_hint():
        """Repaint the output-mode control from what is actually stored.

        The Add page is built once, so a mode changed elsewhere (a reset, a
        config edit) would leave the visible toggle showing the old choice.
        The shell calls this when the user navigates to Add.
        """
        try:
            st = load_settings()
            blurb = mode_refs.get("blurb")
            if blurb is not None:
                blurb.value = MODE_BLURB.get(st.output_mode, MODE_BLURB["1"])
            paint = mode_refs.get("paint")
            if paint is not None:
                paint(st.output_mode if st.output_mode in MODE_BLURB else "1")
        except Exception:
            pass

    # ------------------------------------------------------------ Add
    def downloads_view(page: ft.Page):
        url_count_txt = ui.text("", role=ui.ROLE_META)
        # The count belongs to the FIELD, not to the action row: it says what
        # the links below it will do. It hides itself when there is nothing
        # to say, so an empty line never holds the page open.
        url_count_row = ui.cluster([url_count_txt], spacing=ui.S2)
        url_count_row.visible = False

        def _on_url_change(e):
            urls = _re_module.findall(r"https?://t\.me/\S+", url_box.value or "")
            n = len(urls)
            url_count_txt.value = (f"{n} URL{'s' if n != 1 else ''} detected"
                                   if n else "")
            url_count_row.visible = bool(n)
            try:
                page.update()
            except Exception:
                pass

        # No expand=True here. min_lines/max_lines give the field a definite
        # height, and a TextField with expand under the page's scrolling
        # Column takes its whole subtree down with it (see ui.text_field).
        # ui.field() stretches it across the width instead.
        url_box = ui.text_field(
            hint="https://t.me/channel/123\n(one per line)",
            multiline=True, min_lines=5, max_lines=10,
            on_change=_on_url_change)

        status_txt = ui.text("", role=ui.ROLE_META)
        # The whole row hides when there is nothing to say. Hiding only the
        # text left a blank band holding an empty line.
        status_row = ui.cluster([ui.state_dot(ui.STATUS_WAITING, size=7),
                                 status_txt], spacing=ui.S2)
        status_row.visible = False

        def _set_status(text: str):
            """Set the status line, hiding the row entirely when it is empty.

            One writer, so no call site has to remember to clear both the
            text and the row that holds it.
            """
            status_txt.value = text or ""
            status_row.visible = bool(text)
            try:
                page.update()
            except Exception:
                pass

        async def ensure_api_or_goto_settings():
            st = load_settings()
            if not st.api_id or not st.api_hash:
                # Index 2 is the Account auth tab, which is where the API
                # credentials actually live -- saying "Settings" sent the
                # user to a page that no longer holds them.
                snack(page, "First add your Telegram API ID + hash in "
                            "Account auth.", error=True)
                if "goto" in nav:
                    nav["goto"](2)
                return None
            return st

        async def on_add(e):
            """Paste links, press Add, done.

            The worker fetches each item's message anyway -- there is no file
            to download without one -- so items go straight into the queue
            with their URL, the worker names each file from the message it
            receives, and the user pastes and presses one button.
            """
            if state["queuing"]:
                return  # double-click / repeat click must not double-queue
            st = await ensure_api_or_goto_settings()
            if not st:
                return
            urls = split_urls(url_box.value or "")
            if not urls:
                snack(page, "Paste at least one t.me link first.", error=True)
                return
            state["queuing"] = True
            _set_status(f"Adding {len(urls)} link(s)\u2026")
            try:
                items = [DownloadItem(url=u, mode=st.output_mode or "1",
                                      status="queued")
                         for u in urls]
                added = store.extend(items)
                if len(added) < len(items):
                    snack(page,
                          f"Queue near full: added {len(added)} of "
                          f"{len(items)} link(s).", error=True)
                else:
                    snack(page, f"Downloading {len(added)} link(s).")
                url_box.value = ""
                _on_url_change(None)
            finally:
                state["queuing"] = False
                _set_status("")
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
                    # No symlink/fifo bomb and no TOCTOU: open
                    # O_NOFOLLOW|O_RDONLY first, then fstat the fd (a
                    # swapped-in link raises ELOOP and the type/size checks
                    # run on the opened file itself).
                    import os as _os
                    import stat as _stat
                    import errno as _errno
                    nofollow = getattr(_os, "O_NOFOLLOW", 0)
                    try:
                        fd = _os.open(fpath, _os.O_RDONLY | nofollow)
                    except OSError as exc:
                        if exc.errno == _errno.ELOOP:
                            snack(page, "File must be a regular file "
                                       "(symlink refused).", error=True)
                            return
                        snack(page, f"Cannot open file: {exc}", error=True)
                        return
                    try:
                        info = _os.fstat(fd)
                        if not _stat.S_ISREG(info.st_mode):
                            snack(page, "File must be a regular file.",
                                  error=True)
                            return
                        if info.st_size > MAX_BATCH_BYTES:
                            snack(page, "File too large (max 256 KB). "
                                       "Split it or trim it.", error=True)
                            return
                        with _os.fdopen(fd, "r", encoding="utf-8",
                                        errors="replace") as fh:
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
                    _on_url_change(None)
                    snack(page, f"Loaded {len(urls)} URL(s) from file.")
                page.update()
            except Exception as exc:
                snack(page, f"Cannot read file: {exc}", error=True)

        def on_clear(e):
            url_box.value = ""
            _on_url_change(None)
            _set_status("")
            page.update()

        # --- output mode: one writer, one visible control -----------------
        # The mode is what happens to the file AFTER it lands, so it sits
        # with the links it applies to and above the Add button that starts
        # the download. It used to live in Settings, which made the user go
        # somewhere else to learn what the app was about to do to their
        # files, and then come back to press the button.
        #
        # The toggle is rebuilt whenever the stored mode changes from what
        # is painted, so a Reset (or any future writer) cannot leave the
        # visible segments showing the old mode.
        _st0 = load_settings()
        _mk = _st0.output_mode if _st0.output_mode in MODE_BLURB else "1"
        mode_blurb_txt = ui.text(MODE_BLURB[_mk], role=ui.ROLE_META)
        shown_mode = {"value": None}
        mode_toggle_slot = ft.Container()

        def _paint_mode(val: str):
            if shown_mode["value"] == val:
                return
            shown_mode["value"] = val
            mode_toggle_slot.content = ui.toggle(
                [("1", "Original"), ("2", "Fast"), ("3", "Max")], val,
                _on_mode_pick)

        def _apply_mode(val: str):
            """Single writer for the output mode."""
            s = load_settings()
            s.output_mode = val
            save_settings(s)
            shown_mode["value"] = None
            _paint_mode(val)
            refresh_mode_hint()

        def _on_mode_pick(val: str):
            _apply_mode(val)
            try:
                page.update()
            except Exception:
                pass

        # The reset path in Settings drives the VISIBLE control through this
        # handle, so a reset cannot leave the old mode on screen. The views
        # are built in list order and downloads_view is first, so the handle
        # exists by the time Settings is built.
        state["mode_set"] = _apply_mode
        mode_refs["blurb"] = mode_blurb_txt
        mode_refs["paint"] = _paint_mode
        _paint_mode(_mk)

        # -- The action row: two arrangements of the same three buttons -----
        # A row of three fixed-width buttons fits a desktop and does NOT fit
        # a 360px phone: the last one ran off the right edge of the viewport
        # and could not be pressed. flet 0.86.5 cannot simply wrap them
        # either -- a Container with on_click inside a wrapping Row renders
        # as a grey error box (every variant of it, with the alignment, the
        # ink, the animation and the click handler each removed in turn) --
        # so the narrow arrangement is a COLUMN, and the shell shows the one
        # that fits (see _apply_responsive).
        def _add_actions():
            return [
                ui.action("Add", icon=ft.Icons.ADD, kind="primary",
                          on_click=on_add),
                ui.action("Load .txt", icon=ft.Icons.FILE_OPEN,
                          kind="secondary", on_click=on_load_file),
                ui.action("Clear", icon=ft.Icons.CLEAR, kind="secondary",
                          on_click=on_clear),
            ]

        add_actions_wide = ui.cluster(_add_actions(), spacing=ui.S2)
        add_actions_narrow = ui.stack(_add_actions(), spacing=ui.S2)
        add_actions_narrow.visible = False   # the shell picks the one that fits
        layout_refs["add"] = (add_actions_wide, add_actions_narrow)

        return ui.stack([
            ui.field(url_box, label="Telegram link(s)"),
            url_count_row,
            _heading("Output mode"),
            ui.stack([
                ui.cluster([mode_toggle_slot], spacing=ui.S2, fill=True),
                mode_blurb_txt,
            ], spacing=ui.S2),
            add_actions_wide,
            add_actions_narrow,
            status_row,
        ], spacing=ui.S4, scroll=ft.ScrollMode.AUTO, expand=True, stretch=True)

    def open_folder(page):
        """Reveal the download folder in the desktop file manager."""
        def _redact_home(p: Path) -> str:
            try:
                home = str(Path.home())
                ps = str(p)
                return ("~" + ps[len(home):]
                        if ps.startswith(home + "/") or ps == home else ps)
            except Exception:
                return str(p)
        p = ensure_download_dir(load_settings())
        if not p.is_absolute():
            snack(page, f"Cannot use this folder: {_redact_home(p)}", error=True)
            return
        try:
            # No `--`: macOS open(1) and old xdg-open don't support bare `--`,
            # and is_absolute() already kills option-injection. Resolved to
            # absolute paths: hostile $PATH must not swap them.
            import shutil as _shutil
            if sys.platform.startswith("linux"):
                exe = _shutil.which("xdg-open")
                if not exe:
                    snack(page, "Cannot open folder: xdg-open not found.",
                          error=True)
                    return
                subprocess.Popen([exe, str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen([_shutil.which("open") or "/usr/bin/open",
                                  str(p)])
            elif sys.platform == "win32":
                # explorer parses `,` / `/select,` internally; reject odd
                # chars earlier in ensure_download_dir, keep list-form here.
                subprocess.Popen(["explorer", str(p)])
            else:
                snack(page, f"Folder: {p}")
        except Exception as exc:
            snack(page, f"{p} ({exc})")

    async def pick_download_dir(page) -> bool:
        """Ask for a download folder, validate it, and store it.

        Lives at run_gui scope because BOTH the bottom bar and the Settings
        page change the folder, and they must not disagree: the bar shows the
        path, the Settings row shows it too, and one saved value feeds both.
        Returns True when the setting actually changed.
        """
        picker = _page_service(page, ft.FilePicker)
        try:
            chosen = await picker.get_directory_path(
                dialog_title="Choose download folder")
        except Exception as exc:
            snack(page, f"Could not open folder picker: {exc}", error=True)
            return False
        if not chosen:
            return False
        # Validate before storing: ensure_download_dir(st) rejects paths
        # inside the config dir, filesystem roots, and paths with shell
        # metacharacters, so neither the bar nor Settings can display a
        # folder that download time would then refuse.
        try:
            st = load_settings()
            st.download_dir = str(chosen)
            ensure_download_dir(st)
            save_settings(st)
        except Exception as exc:
            snack(page, f"Cannot use this folder: {exc}", error=True)
            return False
        snack(page, "Download folder updated.")
        return True

    # ------------------------------------------------------------ queue engine
    async def start_queue(page: ft.Page):
        if state["downloading"]:
            return
        if not any(i.status == "queued" for i in store.items):
            snack(page, "Queue is empty (or all done). "
                        "Retry an error or add new links.")
            return
        state["downloading"] = True
        state["cancel"] = asyncio.Event()
        refresh_queue()
        try:
            c = await get_client(page)
            st = load_settings()
            out_dir = ensure_download_dir(st)
            default_mode = st.output_mode or "1"
            # Re-drain: pick queued items one at a time rather than a
            # snapshot, so "Retry" or "Resume all" during a run is honoured
            # instead of lost.
            while not state["cancel"].is_set():
                item = next((i for i in store.items if i.status == "queued"),
                            None)
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
                    from .telegram_utils import auto_title_from_message
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
                        # Race the metadata fetch against Cancel: without
                        # this a stuck get_messages holds the item
                        # "downloading" for up to 120 s with no way to stop.
                        _mget = asyncio.ensure_future(c.get_messages(chat,
                                                                     ids=mid))
                        _cwait = asyncio.ensure_future(state["cancel"].wait())
                        done, _pending = await asyncio.wait(
                            {_mget, _cwait},
                            return_when=asyncio.FIRST_COMPLETED, timeout=120)

                        async def _cancel_pending():
                            for t in (_mget, _cwait):
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(*_pending,
                                                 return_exceptions=True)

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
                                "Telegram didn't respond within 2 minutes. "
                                "The service may be busy — please try again "
                                "shortly.")
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError(
                            "Telegram didn't respond within 2 minutes. "
                            "The service may be busy — please try again "
                            "shortly.") from exc
                    if not msg or not getattr(msg, "media", None):
                        raise RuntimeError("Message has no downloadable media.")
                    # Name the file from the message we just fetched, not from
                    # a title the user had to set on a preview screen: the Add
                    # tab previews nothing, so item.title is normally empty
                    # and every file would otherwise land as msg-12345.mp4.
                    # infer_extension() has to run BEFORE the store write --
                    # computing it after raised NameError, and the swallowed
                    # exception meant title and size were never persisted.
                    ext = infer_extension(msg)
                    stem = safe_filename(item.title
                                         or auto_title_from_message(msg, chat))
                    try:
                        store.update(item.id, title=stem, ext=ext,
                                     size=int(getattr(getattr(msg, "file", None),
                                                      "size", 0) or 0))
                    except Exception:
                        pass
                    source = out_dir / f"{stem}{ext}"
                    target = out_dir / f"{stem}.mp4"
                    try:
                        out_res = out_dir.resolve()
                        if source.is_symlink() or target.is_symlink() or \
                                source.absolute().resolve().parent != out_res or \
                                target.absolute().resolve().parent != out_res:
                            raise RuntimeError("Refusing to write outside "
                                               "the download folder.")
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
                            store.update(item.id, status="done", progress=100.0,
                                         dest=str(source),
                                         error="ffmpeg missing — kept original.")
                            continue
                        store.update(item.id, status="converting")
                        refresh_queue()
                        import os as _os
                        import secrets as _secrets
                        import threading as _threading
                        tmp = target.with_name(
                            f"{target.stem}.part.{_os.getpid()}."
                            f"{_secrets.token_hex(8)}.mp4")
                        nofollow = getattr(_os, "O_NOFOLLOW", 0)
                        try:
                            fd = _os.open(tmp, _os.O_WRONLY | _os.O_CREAT
                                          | _os.O_EXCL | nofollow, 0o600)
                            _os.close(fd)
                            if tmp.is_symlink():
                                raise RuntimeError(
                                    "Refusing to publish through symlink.")
                        except FileExistsError:
                            raise RuntimeError("Refusing unsafe converter "
                                               "temp file (exists).")
                        except OSError as exc:
                            import errno as _errno
                            if exc.errno == _errno.ELOOP:
                                raise RuntimeError("Refusing to publish "
                                                   "through symlink.") from exc
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
                            raise RuntimeError(
                                "Refusing to publish through symlink.")
                        if target.is_symlink():
                            try:
                                tmp.unlink(missing_ok=True)
                            except OSError:
                                pass
                            raise RuntimeError("Refusing to publish through "
                                               "symlink at target path.")
                        tmp.replace(target)
                        try:
                            if not source.is_symlink():
                                source.unlink(missing_ok=True)
                        except OSError:
                            pass
                        store.update(item.id, status="done", progress=100.0,
                                     dest=str(target))
                    else:
                        store.update(item.id, status="done", progress=100.0,
                                     dest=str(source))
                except Cancelled as exc:
                    store.update(item.id, status="cancelled",
                                 error=_sanitize_display(str(exc)))
                except Exception as exc:
                    if state["cancel"].is_set():
                        store.update(item.id, status="cancelled",
                                     error="Cancelled by user.")
                    else:
                        store.update(item.id, status="error",
                                     error=_sanitize_display(str(exc)))
                refresh_queue()
        except Exception as exc:
            snack(page, _sanitize_display(f"Download failed: {exc}"), error=True)
        finally:
            state["downloading"] = False
            refresh_queue()

    def _build_row(page, it):
        """One queue row, built from ui primitives.

        A row is a line of the list, not a card: `ui.list_row` gives it the
        hairline-only grid, the hover wash and the leading status dot.
        `leading=` is passed instead of `state=` on purpose -- list_row's
        `state=` path appends a progress bar of its own with expand=True,
        and the bar here has to stay under our handle so it can be updated
        in place.
        """
        ui_status = _STORE_TO_UI.get(it.status, ui.STATUS_WAITING)
        title = ui.text(_sanitize_display(f"{it.title}{it.ext}", limit=120),
                        role=ui.ROLE_TITLE, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS)

        progress_bar = None
        detail_txt = None
        if it.status in ("downloading", "converting", "fetching"):
            bar_value = (None if it.status == "fetching"
                         else (it.progress or 0) / 100)
            progress_bar = ui.progress(bar_value, state=ui.STATUS_WORKING,
                                       expand=False)
            detail_txt = ui.text(_progress_detail(it), role=ui.ROLE_META)
            meta = ui.stack([detail_txt, progress_bar], spacing=ui.S1,
                            stretch=True)
        elif it.status in ("queued", "cancelled"):
            detail_txt = ui.status_line(ui_status, _IDLE_LABEL.get(it.status))
            meta = detail_txt
        elif it.status == "done" and it.dest:
            meta = ui.text(_sanitize_display(it.dest), role=ui.ROLE_META,
                           mono=True, max_lines=1,
                           overflow=ft.TextOverflow.ELLIPSIS)
        elif it.status == "error" and it.error:
            meta = ui.cluster(
                [ui.state_dot(ui.STATUS_FAILED, size=7),
                 _grow(ui.text(_sanitize_display(it.error), role=ui.ROLE_META,
                               color=ft.Colors.ERROR, max_lines=2,
                               overflow=ft.TextOverflow.ELLIPSIS), 1)],
                spacing=ui.S2)
        elif it.status == "done" and it.error:
            # Kept-original note (e.g. ffmpeg missing) -- neutral, not loud.
            meta = ui.text(_sanitize_display(it.error), role=ui.ROLE_META,
                           max_lines=2)
        else:
            meta = None

        actions = []
        if it.status in ("error", "cancelled"):
            actions.append(ui.icon_action(
                ft.Icons.REFRESH, "Retry",
                lambda e, _id=it.id: (store.update(_id, status="queued",
                                                   error=""),
                                      start_soon(page))))
        if it.status in ("downloading", "converting", "fetching"):
            actions.append(ui.icon_action(
                ft.Icons.STOP_CIRCLE_OUTLINED,
                "Pause this download (it resumes from where it stopped)",
                lambda e, _id=it.id: toggle_pause(page, _id)))
        else:
            actions.append(ui.icon_action(
                ft.Icons.CLOSE, "Remove",
                lambda e, _id=it.id: (store.remove(_id), refresh_queue()),
                kind="danger"))

        row = ui.list_row(leading=ui.state_dot(ui_status, size=9),
                          title=title, meta=meta, trailing=actions)
        row.border = ft.Border(bottom=ft.BorderSide(1, ui.HAIRLINE))
        _queue_widgets[it.id] = {"card": row, "progress_bar": progress_bar,
                                 "detail_txt": detail_txt, "sig": _row_sig(it)}
        return row

    queue_list = None  # set by queue_view; refreshed from anywhere
    queue_header_txt = {"ctrl": None, "summary": None}

    def refresh_queue():
        nonlocal _queue_empty_shown
        # The page comes from _main_page, NOT from queue_list.page.
        #
        # Probing the control (`queue_list.page`) is wrong in the one place
        # it matters: on_nav() assigns body_content.content = views[idx] and
        # then calls refresh_queue(), but a Flet tree is not mounted until
        # page.update() runs, so the freshly-selected view is still detached
        # at that instant -- every tab switch to Queue bailed out and the
        # list stayed empty. Rendering into a detached view is harmless: the
        # controls are built either way and serialised when it mounts.
        page = _main_page
        if page is None:
            # _main has not run yet (build-time construction). Nothing to
            # repaint, but keep the store's on-disk copy current.
            try:
                store.save()
            except Exception:
                pass
            return
        items = list(store.items)
        # Active + queued first, finished at the bottom (newest first within
        # each group).
        idx = {it.id: k for k, it in enumerate(store.items)}
        _rank = {"downloading": 0, "converting": 1, "fetching": 2,
                 "queued": 3, "done": 4, "cancelled": 5, "error": 6}
        items.sort(key=lambda i: (_rank.get(i.status, 7), -idx.get(i.id, 0)))
        n_total = len(items)
        n_active = sum(1 for i in items
                       if i.status in ("downloading", "converting", "fetching"))
        n_queued = sum(1 for i in items if i.status == "queued")
        n_failed = sum(1 for i in items if i.status in ("error", "cancelled"))
        n_done = sum(1 for i in items if i.status == "done")

        # The count goes through the SHARED handle, never a queue_view local:
        # the two functions are siblings under run_gui, so a queue_view local
        # is a NameError from here (it used to raise name 'header' is not
        # defined on every empty-queue refresh).
        ctrl = queue_header_txt["ctrl"]
        if ctrl is not None:
            ctrl.value = f"Downloads ({n_total})"
        summary = queue_header_txt["summary"]
        if summary is not None:
            summary.spans = []
            summary.value = ""
            if not items:
                summary.value = "Nothing here yet."
            else:
                # Only render counters that are non-zero: "0 need attention"
                # put an alarm signal on a healthy queue. Plain text, no
                # fills -- only "need attention" keeps its colour, because
                # that one is asking for something.
                def _count(n, label, color):
                    if n <= 0:
                        return []
                    return [ft.TextSpan(f"{n} {label}  ",
                                        ft.TextStyle(color=color)),
                            ft.TextSpan(" ")]
                summary.spans = [
                    *_count(n_active, "active", ft.Colors.ON_SURFACE_VARIANT),
                    *_count(n_queued, "queued", ft.Colors.ON_SURFACE_VARIANT),
                    *_count(n_done, "done", ft.Colors.ON_SURFACE_VARIANT),
                    *_count(n_failed, "need attention", ft.Colors.ERROR),
                ]
                if not summary.spans:
                    summary.value = "All clear."

        # Queue tab badge: what is moving plus what needs attention.
        badge_count = n_active + n_failed
        dest = queue_header_txt.get("tab")
        if dest is not None:
            try:
                dest.badge = (ft.Badge(str(badge_count),
                                       bgcolor=ft.Colors.ERROR)
                              if badge_count > 0 else None)
            except Exception:
                pass

        current_ids = {it.id for it in items}
        for old_id in list(_queue_widgets.keys()):
            if old_id not in current_ids:
                del _queue_widgets[old_id]

        # The empty state is a SIBLING of the ListView (see queue_empty in
        # queue_view), not a child of it: refresh_queue() runs while
        # queue_view() is still building, so an appended child would be
        # mounted by a page.update() that has no page to update yet. Toggling
        # a sibling's `visible` needs no such thing.
        queue_empty.visible = not items
        queue_list.visible = bool(items)

        if not items:
            if not _queue_empty_shown:
                queue_list.controls.clear()
                _queue_empty_shown = True
                try:
                    page.update()
                except Exception:
                    pass
            return

        if _queue_empty_shown:
            queue_list.controls.clear()
            _queue_empty_shown = False

        new_controls = []
        for it in items:
            sig = _row_sig(it)
            widgets = _queue_widgets.get(it.id)
            if widgets is not None and widgets["sig"] == sig:
                # Same content: update the live widgets in place instead of
                # rebuilding the row (which would drop the hover state and
                # re-layout the whole list four times a second).
                bar = widgets["progress_bar"]
                if bar is not None:
                    bar.value = (None if it.status == "fetching"
                                 else max(0.0, min(1.0, (it.progress or 0) / 100)))
                detail = widgets["detail_txt"]
                if detail is not None:
                    detail.value = _progress_detail(it)
                new_controls.append(widgets["card"])
                continue
            new_controls.append(_build_row(page, it))
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
            store.update(target_id, status="cancelled",
                         error="Cancelled by user.")
            refresh_queue()
            snack(page, "Removed queued download.")
            return
        if it.status in ("downloading", "converting", "fetching"):
            # Only one batch can run at a time, so stopping the active item
            # naturally pauses the run. Be honest about it: the current file
            # is cancelled and the rest remain queued for "Resume all / Start".
            if state.get("cancel") and not state["cancel"].is_set():
                state["cancel"].set()
                snack(page, "Stopping this download — others stay queued "
                            "(Resume all to continue).")
            else:
                snack(page, "Already stopping\u2026", error=True)

    def toggle_pause(page, target_id):
        """Pause an active download, or resume a paused one.

        Users read the progress bar as a control, so clicking it has to do
        something. Pause goes through cancel_id, which sets the run's cancel
        event -- drastic, but it is the only stop primitive the engine has,
        and download_resumable() keeps the <name>.part plus its sidecar so
        the next run continues from the byte offset instead of starting over.
        """
        it = store.get(target_id)
        if not it:
            return
        if it.status in ("downloading", "converting", "fetching"):
            cancel_id(page, target_id)
            return
        if it.status == "cancelled":
            # Re-queue and let start_queue's re-drain pick it up, clearing
            # the error so the row does not still read "cancelled".
            store.update(target_id, status="queued", error="")
            refresh_queue()
            snack(page, "Resuming — continues from where it stopped.")
            start_soon(page)
            return
        snack(page, "Only a running or paused download can be paused.",
              error=True)

    # Bound before queue_view/refresh_queue can run: refresh_queue() reads and
    # writes the first two, and _main() is what sets _main_page.
    _queue_empty_shown = False
    queue_empty = ft.Container()
    _main_page = None

    def queue_view(page: ft.Page):
        nonlocal queue_list, _queue_empty_shown, queue_empty, queue_toolbar_summary_ref
        _queue_empty_shown = False
        queue_list = ft.ListView(spacing=0, expand=True)
        queue_empty = ui.empty(
            ft.Icons.DOWNLOAD_DONE, "No downloads in queue",
            "Add links from the Add tab to get started.",
            action=ui.action("Add downloads", icon=ft.Icons.ADD,
                             kind="primary",
                             on_click=(lambda e: nav["goto"](0)
                                       if "goto" in nav else None)))
        # The header and summary are registered through the shared handles:
        # refresh_queue() is a sibling and cannot see this function's locals.
        #
        # ORDER MATTERS: the control has to be CREATED before the handle is
        # taken. Registering first handed the toolbar the previous (or a
        # never-set) value, so _grow() was handed None and the app died on
        # startup with "'NoneType' object has no attribute 'expand'".
        queue_header_txt["ctrl"] = ui.text(f"Downloads ({len(store.items)})",
                                           role=ui.ROLE_STRONG)
        queue_header_txt["summary"] = ui.text("", role=ui.ROLE_META,
                                              align=ft.TextAlign.RIGHT)
        queue_toolbar_summary_ref = queue_header_txt["summary"]

        async def on_resume_all(e):
            store.reset_errors_to_queued()
            refresh_queue()
            await start_queue(page)

        def on_cancel(e):
            if state.get("cancel"):
                state["cancel"].set()
                snack(page, "Stopping queue — current file is cancelled, "
                            "the rest stay queued.")

        toolbar = ui.cluster([
            queue_header_txt["ctrl"],
            _grow(queue_toolbar_summary_ref, 1),
            ui.action("Clear finished", icon=ft.Icons.DELETE_SWEEP,
                      kind="quiet",
                      on_click=lambda e: (store.clear_finished(),
                                          refresh_queue())),
            ui.action("Resume all / Start", icon=ft.Icons.PLAY_ARROW,
                      kind="secondary", on_click=on_resume_all),
            ui.action("Cancel batch", icon=ft.Icons.STOP_CIRCLE_OUTLINED,
                      kind="secondary", on_click=on_cancel),
        ], spacing=ui.S2, fill=True)
        refresh_queue()
        return ui.stack([
            toolbar,
            queue_empty,
            queue_list,
        ], spacing=ui.S4, expand=True, stretch=True)

    # ------------------------------------------------------------ settings
    # There is no settings_refs dict and no refresh_settings_hint() any
    # more. The output mode was the only thing either of them repainted,
    # and the output mode now lives on the Add page, with its own handles
    # (mode_refs / refresh_mode_hint) declared next to it.


    # ------------------------------------------------------------ auth
    def auth_view(page: ft.Page):
        """Focused authentication view: API credentials + login/logout."""
        st = load_settings()
        first = not st.api_id or not st.api_hash

        api_id_f = ui.text_field(value=st.api_id or "")
        api_hash_f = ui.text_field(value=st.api_hash or "", password=True,
                                   reveal=True)

        auth_error = ft.Container(visible=False)

        async def on_save(e):
            s = load_settings()
            s.api_id = api_id_f.value.strip() or None
            s.api_hash = api_hash_f.value.strip() or None
            if s.api_id and not s.api_id.isdigit():
                auth_error.content = ui.notice("API ID must be numeric.",
                                               kind="error")
                auth_error.visible = True
                page.update()
                return
            save_settings(s)
            auth_error.visible = False
            refresh_mode_hint()
            page.update()
            snack(page, "Credentials saved.")

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
                    name = (getattr(me, "first_name", None)
                            or getattr(me, "username", None))
                    snack(page, f"Already logged in as "
                                f"{_sanitize_display(str(name))}.")
                    return
                ok = await gui_login(page, c)
                snack(page, "Login OK." if ok else "Login cancelled.")
            except Exception as exc:
                snack(page, f"Login failed: {exc}", error=True)

        async def on_logout(e):
            """Sign out of Telegram.

            Matches `tg-dl config logout` exactly, because the two are the
            same operation and must not drift: the API id/hash belong to the
            APP and are kept, the pinned identity is cleared, and the local
            session file is removed.
            """
            server_ok = False
            try:
                c = await get_client(page)
                await c.log_out()
                server_ok = True
            except Exception:
                pass
            # The pin is what stops a planted session from being adopted, so
            # signing out has to clear it -- otherwise the one escape hatch
            # the pin documents ("Log out, then sign in again") stays
            # blocked, and the user is stuck with an account they cannot
            # replace.
            try:
                st = load_settings()
                if getattr(st, "user_id", None):
                    st.user_id = None
                    save_settings(st)
            except Exception:
                pass
            try:
                clear_session()
            except Exception:
                pass
            try:
                harden_session_files()
            except Exception:
                pass
            refresh_mode_hint()
            # A local-only logout leaves the session alive on Telegram's
            # side. Say so plainly instead of implying a clean sign-out.
            leftover = False
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
                      "(offline?). Revoke at my.telegram.org if shared.",
                      error=True)
            else:
                snack(page, "Logged out — server session revoked, "
                            "files removed.")

        sections = [
            _heading("Telegram account",
                     trailing=ui.badge("UNSET" if first else "STORED"))]
        if first:
            sections.append(ui.notice(
                "Create these at my.telegram.org → API development tools. "
                "They are stored privately (0600) and only asked once."))

        # Account fields: ResponsiveRow stacks on phones (xs) and
        # sits two-across on tablets (md); no `expand` children, so
        # no grey-box trap -- each column owns a col span.
        api_id_field = ui.field(api_id_f, label="API ID",
                                hint="my.telegram.org → App api_id")
        api_hash_field = ui.field(api_hash_f, label="API hash",
                                   hint="128-bit hex string")
        api_id_field.col = {"xs": 12, "md": 6}
        api_hash_field.col = {"xs": 12, "md": 6}
        # Two arrangements of the same three actions, for the same reason as
        # the Add page's row: a row of fixed-width buttons does not fit a
        # 360px phone, and a wrapping Row cannot hold them in flet 0.86.5.
        def _auth_actions():
            return [
                pending_button("Save", ft.Icons.SAVE, on_save,
                               kind="primary"),
                pending_button("Verify / Login", ft.Icons.LOGIN, on_login,
                               kind="secondary"),
                pending_button("Log out", ft.Icons.LOGOUT, on_logout,
                               kind="secondary"),
            ]

        auth_actions_wide = ui.cluster(_auth_actions(), spacing=ui.S2)
        auth_actions_narrow = ui.stack(_auth_actions(), spacing=ui.S2)
        auth_actions_narrow.visible = False  # the shell picks the one that fits
        layout_refs["auth"] = (auth_actions_wide, auth_actions_narrow)

        sections += [
            ui.stack([
                ft.ResponsiveRow(
                    [
                        api_id_field,
                        api_hash_field,
                    ],
                    spacing=ui.S3,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                auth_error,
                auth_actions_wide,
                auth_actions_narrow,
            ], spacing=ui.S4),
        ]
        return ui.stack(sections, spacing=ui.S6, scroll=ft.ScrollMode.AUTO,
                        expand=True, stretch=True)

    # ------------------------------------------------------------ settings
    def settings_view(page: ft.Page):
        # The output mode used to be built here. It now lives on the Add
        # page, next to the links it applies to. The only thing Settings
        # still does with the mode is reset it, and it drives the visible
        # control through the state["mode_set"] handle the Add page
        # registers.

        async def on_check_updates(e):
            # No button bookkeeping here: pending_button() owns the busy
            # state (disabled + spinner for the whole await chain), so the
            # handler only has to do the work.
            try:
                info = await asyncio.to_thread(updates.check_for_update)
                if info is None:
                    snack(page, "Could not reach the update server.",
                          error=True)
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
                snack(page, f"Update check failed: "
                            f"{_sanitize_display(str(exc))}", error=True)

        async def download_installer(page, info):
            asset = updates.installable_asset(info)
            if asset is None:
                # .dmg needs a mounted image and a drag-to-Applications step;
                # .apk is an Android package a desktop cannot install. Send
                # the user to the release page instead of pretending.
                snack(page, "No installer for your OS -- opening release "
                            "page instead.", error=True)
                try:
                    updates.open_release_page(info.release_url)
                except Exception:
                    pass
                return
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
            # its own confirmation -- never fused silently into the download.
            go = await confirm_dialog(
                page,
                "Install this update?",
                f"Downloaded and verified:\n"
                f"{_sanitize_display(os.path.basename(str(dest)))}\n\n"
                "Installing runs apt-get with administrator rights. Your "
                "normal password prompt will appear. The app should be closed "
                "while it updates.",
                action="Install and restart", icon=ft.Icons.INSTALL_MOBILE)
            if not go:
                snack(page, f"Installer saved to {dest}.")
                return
            snack(page, "Installing\u2026 approve the password prompt to "
                        "continue.")
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
                ui.key_value("Version", f"v{app_version()}"),
                ui.key_value("Engine", f"Telethon {tver}"),
                ui.key_value("FFmpeg", ff, mono=True),
            ]

        async def on_reset(e):
            # Destructive and irreversible: clears the saved Telegram
            # session, the credentials and the whole queue. A plain yes/no
            # sat one stray click away from wiping a logged-in session, so
            # this requires typing the word.
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
            try:
                # Drive the visible toggle, not a hidden control: _apply_mode
                # repaints the segmented control as well as the stored value,
                # so a reset cannot leave the old mode on screen.
                ms = state.get("mode_set")
                if ms is not None:
                    ms("1")
            except Exception:
                pass
            refresh_queue()
            refresh_mode_hint()
            snack(page, "Reset complete. Re-enter API credentials.")

        # --- layout -------------------------------------------------------
        sections = [
            # Output mode
            # NOT HERE. It lives on the Add page, next to the links it
            # applies to, so the choice is made in the same breath as the
            # download it changes.

            # Downloads folder
            # NOT HERE. The download folder is chosen from the bottom bar,
            # which is always on screen, and having a second copy of the
            # same control in Settings meant two places to change one
            # value and one of them could be a step out of date.

            # Dependencies
            _heading("Dependencies"),
            deps_section(page),

            # About
            _heading("About"),
            ui.stack([
                ui.stack(_about_rows(), spacing=ui.S1),
                # The in-app updater. The whole machinery -- on_check_updates,
                # update_dialog, download_installer, install_deb -- survived
                # the rewrite, but the BUTTON that called it did not, so the
                # app had no way to check for updates at all and the suite
                # stayed green. A missing affordance is invisible to a test
                # that only imports functions, so it is pinned by a
                # structural test next to this one.
                # In a cluster, not straight into the column: a button with
                # an alignment takes the full width of a column, which made
                # this one a slab across the page while the Re-check button
                # above it is the size of its label.
                ui.cluster([
                    pending_button("Check for updates", ft.Icons.UPDATE,
                                   on_check_updates, kind="secondary")],
                    fill=True),
            ], spacing=ui.S3),
            # Reset
            _heading("Danger zone"),
            ui.stack([
                ui.notice("This signs you out of Telegram, deletes your saved API "
                          "credentials, and empties the download queue. Files already "
                          "downloaded are not touched. This cannot be undone.",
                          kind="info"),
                pending_button("Reset everything", ft.Icons.WARNING_AMBER,
                               on_reset, kind="danger"),
            ], spacing=ui.S3),
        ]
        return ui.stack(sections, spacing=ui.S6, scroll=ft.ScrollMode.AUTO,
                        expand=True, stretch=True)

    # ------------------------------------------------------------ deps
    def deps_view(page: ft.Page, header: bool = True):
        col = ui.stack([], spacing=ui.S3, expand=header,
                       scroll=(ft.ScrollMode.AUTO if header else None),
                       stretch=True)
        sys_txt = ui.text(
            f"{platform.system()} {platform.release()} • "
            f"Python {platform.python_version()}", role=ui.ROLE_META)

        def render(rows_data=None):
            col.controls.clear()
            # header=False when embedded as a section inside Settings, which
            # already has its own scroll container and section label.
            if header:
                col.controls.append(sys_txt)
            if rows_data is None:
                # First build happens before the view is shown, so the sync
                # subprocess probes here are acceptable. Repeated checks go
                # through _recheck (thread) to keep the UI responsive.
                rows_data = deps_mod.check_all()
            rows: list = []
            for d in rows_data:
                row = ui.list_row(
                    leading=ui.state_dot(ui.STATUS_DONE if d.ok
                                         else ui.STATUS_FAILED, size=9),
                    title=ui.text(d.name, role=ui.ROLE_STRONG),
                    meta=ui.text(d.hint or "", role=ui.ROLE_META),
                    trailing=ui.cluster([
                        ui.text(d.version or "not detected", role=ui.ROLE_META,
                                mono=True),
                        ui.badge("OK" if d.ok else "MISSING",
                                 tone=(ft.Colors.TERTIARY if d.ok
                                       else ft.Colors.ERROR)),
                    ], spacing=ui.S2))
                row.border = ft.Border(bottom=ft.BorderSide(1, ui.HAIRLINE))
                rows.append(row)
                if not d.ok:
                    # The command is the whole point of a MISSING row, so it
                    # gets its own line with a $ prompt and a Copy button --
                    # users retype these and get them subtly wrong otherwise.
                    cmd = install_cmd_for(d)
                    rows.append(ui.cluster([
                        ui.text("$", role=ui.ROLE_META, mono=True),
                        _grow(ui.text(cmd, role=ui.ROLE_META, mono=True,
                                      selectable=True), 1),
                        ui.action("Copy", icon=ft.Icons.CONTENT_COPY_OUTLINED,
                                  kind="quiet",
                                  on_click=(lambda e, _c=cmd:
                                            _copy_clipboard(page, _c))),
                    ], spacing=ui.S2, fill=True))

            async def _recheck(e):
                # check_all spawns subprocesses (ffprobe -version, etc.):
                # offload to a thread so repeated checks never freeze the UI.
                try:
                    data = await asyncio.to_thread(deps_mod.check_all)
                    render(data)
                except Exception as exc:
                    snack(page, f"Dependency check failed: {exc}", error=True)

            rows.append(ui.cluster([
                pending_button("Re-check", ft.Icons.REFRESH, _recheck,
                               kind="secondary")], fill=True))
            col.controls += rows
            try:
                page.update()
            except Exception:
                pass

        render()
        return col

    def deps_section(page: ft.Page):
        """Dependencies embedded as a section inside Settings.

        It used to be a top-level tab, which made Settings feel incomplete
        ("where is the rest of it?"). Config and environment belong together,
        and this is environment. The inner view does not expand here, so it
        cannot steal Settings' scroll range.
        """
        return ui.stack([deps_view(page, header=False)], spacing=ui.S4)

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

    # ------------------------------------------------------------ login
    async def gui_login(page, client) -> bool:
        """Phone/code/2FA dialogs. Returns True on success."""
        from telethon import errors as terr

        phone_f = ui.text_field(hint="+15551234567 or 12345:ABC-DEF…",
                                autofocus=True)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _cancel(e):
            if not fut.done():
                fut.set_result(None)
            _hide_dlg(page, dlg)

        def _send(e):
            if not fut.done():
                fut.set_result((phone_f.value or "").strip())
            _hide_dlg(page, dlg)

        dlg = ft.AlertDialog(
            title=ui.text("Telegram login", role=ui.ROLE_TITLE),
            icon=ft.Icon(ft.Icons.LOGIN),
            content=ui.field(phone_f, label="Phone number or bot token"),
            actions=[ui.action("Cancel", kind="secondary", on_click=_cancel),
                     ui.action("Send code", kind="primary", on_click=_send)],
        )
        _show_dlg(page, dlg)
        phone = await fut
        _wipe_field(phone_f)
        if not phone:
            return False
        is_phone = tg_client.looks_like_phone(phone)
        # Do NOT pre-send the code: client.start() sends exactly once; the
        # earlier manual send caused a double SMS and invalidated the code.

        async def _code_dialog() -> str:
            # Telethon's default code_callback is blocking input() — that
            # would freeze the asyncio loop. Always answer with a dialog.
            f = ui.text_field(hint="12345", autofocus=True)
            f4: asyncio.Future = asyncio.get_running_loop().create_future()

            def _cancel_code(e):
                if not f4.done():
                    f4.set_result("")
                _hide_dlg(page, d)

            def _ok_code(e):
                if not f4.done():
                    f4.set_result((f.value or "").strip())
                _hide_dlg(page, d)

            d = ft.AlertDialog(
                title=ui.text("Enter the code we just sent",
                              role=ui.ROLE_TITLE),
                icon=ft.Icon(ft.Icons.PASSWORD),
                content=ui.field(f, label="Login code from Telegram"),
                actions=[ui.action("Cancel", kind="secondary",
                                   on_click=_cancel_code),
                         ui.action("OK", kind="primary", on_click=_ok_code)],
            )
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
                    # Pin the account this session belongs to. get_client()
                    # refuses to reuse a session for a different user; the
                    # deliberate way out is "Log out" in Settings.
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
            snack(page, f"Telegram rate-limit: wait {exc.seconds}s.",
                  error=True)
            return False
        except Exception:
            pass
        if not is_phone:
            # Bot tokens never take a login code: don't prompt for one
            # (avoids an unbounded code-guessing loop on tokens).
            snack(page, "Bot login failed. Check the token and try again.",
                  error=True)
            try:
                del phone
            except NameError:
                pass
            return False
        # Fall through: ask code then 2FA password via dialogs (max attempts).
        code = None
        for _attempt in range(3):
            code_f = ui.text_field(hint="12345", autofocus=True)
            fut2: asyncio.Future = asyncio.get_running_loop().create_future()

            def _cancel_code2(e):
                if not fut2.done():
                    fut2.set_result(None)
                _hide_dlg(page, dlg2)

            def _confirm_code(e):
                if not fut2.done():
                    fut2.set_result((code_f.value or "").strip())
                _hide_dlg(page, dlg2)

            dlg2 = ft.AlertDialog(
                title=ui.text("Enter code", role=ui.ROLE_TITLE),
                icon=ft.Icon(ft.Icons.PASSWORD),
                content=ui.field(code_f, label="Login code from Telegram"),
                actions=[ui.action("Cancel", kind="secondary",
                                   on_click=_cancel_code2),
                         ui.action("Confirm", kind="primary",
                                   on_click=_confirm_code)],
            )
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
                snack(page, f"Telegram rate-limit: wait {exc.seconds}s.",
                      error=True)
                return False
            except Exception as exc:
                if isinstance(exc, terr.SessionPasswordNeededError):
                    break
                if _attempt >= 2:
                    snack(page, "Login failed: too many wrong codes. "
                                "Request a new code.", error=True)
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
            pw_f = ui.text_field(hint="Your 2FA password", password=True,
                                 reveal=True, autofocus=True)
            fut3: asyncio.Future = asyncio.get_running_loop().create_future()

            def _cancel_pw(e):
                if not fut3.done():
                    fut3.set_result(None)
                _hide_dlg(page, dlg3)

            def _login_pw(e):
                if not fut3.done():
                    fut3.set_result(pw_f.value)
                _hide_dlg(page, dlg3)

            dlg3 = ft.AlertDialog(
                title=ui.text("2-step verification", role=ui.ROLE_TITLE),
                icon=ft.Icon(ft.Icons.KEY),
                content=ui.field(pw_f, label="2FA password"),
                actions=[ui.action("Cancel", kind="secondary",
                                   on_click=_cancel_pw),
                         ui.action("Login", kind="primary",
                                   on_click=_login_pw)],
            )
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

    async def typed_confirm_dialog(page, title: str, body: str, phrase: str,
                                   action: str,
                                   icon=ft.Icons.WARNING_AMBER) -> bool:
        """Destructive confirmation the user must complete by typing *phrase*.

        A plain "are you sure?" is one stray click away from deleting a saved
        Telegram session and the whole queue, and the reset button sits right
        under ordinary settings controls where a mis-click is likely. Typing a
        word forces a deliberate act.

        The confirm button carries no handler at all until the text matches
        exactly (ui.action() drops on_click while disabled), so the dialog
        cannot be dismissed by reflex.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _close(result):
            if not fut.done():
                fut.set_result(result)
            _hide_dlg(page, dlg)

        dlg = ft.AlertDialog(
            title=ui.text(title, role=ui.ROLE_TITLE),
            icon=ft.Icon(icon),
        )

        def _paint(ready: bool):
            # Rebuilt, not toggled: ui.action() bakes `disabled` into opacity
            # and on_click at construction, so the only way to arm the button
            # is to hand the dialog a fresh one.
            confirm = ui.action(
                action, kind="primary", disabled=not ready,
                on_click=(None if not ready else lambda e: _close(True)))
            dlg.actions = [ui.action("Cancel", kind="secondary",
                                     on_click=lambda e: _close(False)),
                           confirm]

        def _on_type(e):
            _paint((e.control.value or "").strip() == phrase)
            try:
                page.update()
            except Exception:
                pass

        entry = ui.text_field(hint=phrase, autofocus=True, on_change=_on_type)
        dlg.content = ui.stack([
            ui.text(body, role=ui.ROLE_BODY),
            ui.text(f"Type {phrase} to confirm:", role=ui.ROLE_META),
            entry,
        ], spacing=ui.S3, stretch=True)
        _paint(False)
        _show_dlg(page, dlg)
        return bool(await fut)

    async def confirm_dialog(page, title: str, body: str,
                             action: str = "Download",
                             icon=ft.Icons.DOWNLOAD) -> bool:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _close(result):
            if not fut.done():
                fut.set_result(result)
            _hide_dlg(page, dlg)

        dlg = ft.AlertDialog(
            title=ui.text(title, role=ui.ROLE_TITLE),
            icon=ft.Icon(icon),
            content=ui.text(body, role=ui.ROLE_BODY),
            actions=[ui.action("Cancel", kind="secondary",
                               on_click=lambda e: _close(False)),
                     ui.action(action, kind="primary",
                               on_click=lambda e: _close(True))],
        )
        _show_dlg(page, dlg)
        return bool(await fut)

    async def update_dialog(page, info) -> str:
        """Update-available dialog. Returns 'later' | 'page' | 'download'."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _pick(value: str):
            def _go(e):
                if not fut.done():
                    fut.set_result(value)
                _hide_dlg(page, dlg)
            return _go

        dlg = ft.AlertDialog(
            title=ui.text("Update available", role=ui.ROLE_TITLE),
            icon=ft.Icon(ft.Icons.UPDATE),
            content=ui.text(
                f"Version v{info.latest_version} is available "
                f"(you have v{app_version()}).\n\n"
                "Download the installer now, or open the release page "
                "instead.", role=ui.ROLE_BODY),
            actions=[ui.action("Later", kind="quiet", on_click=_pick("later")),
                     ui.action("Open Release Page", kind="secondary",
                               on_click=_pick("page")),
                     ui.action("Download Installer", kind="primary",
                               on_click=_pick("download"))],
        )
        _show_dlg(page, dlg)
        return str(await fut)

    # ------------------------------------------------------------ shell
    async def _main(page: ft.Page):
        nonlocal _main_page
        _main_page = page
        page.title = "Telegram Downloader"
        # Theme comes from ui.theme(). Colour lives in ui.py and nowhere else;
        # this file used to carry its own palette dict and a hand-built
        # ColorScheme beside it, which is how the chrome and the buttons drifted
        # out of step with each other. The scrollbar theme is set inside
        # ui.theme() too.
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
                # The glyph says where the click GOES, not where you are.
                theme_btn.content.icon = (ft.Icons.LIGHT_MODE
                                          if new_theme == "dark"
                                          else ft.Icons.DARK_MODE)
                theme_btn.tooltip = ("Switch to light mode"
                                     if new_theme == "dark"
                                     else "Switch to dark mode")
            except Exception:
                pass
            try:
                page.update()
            except Exception:
                pass

        theme_btn = ui.icon_action(ft.Icons.LIGHT_MODE,
                                   "Switch to light mode", _toggle_theme)
        # Keep the initial glyph in step with the saved theme.
        if (load_settings().theme or "dark") == "light":
            theme_btn.content.icon = ft.Icons.DARK_MODE
            theme_btn.tooltip = "Switch to dark mode"

        # -- Brand mark ------------------------------------------------------
        # The APP's own logo, not a Telegram paper plane. assets/icon.svg ships
        # inside the package, so the title bar shows the real artwork in every
        # install. It is loaded as a self-contained data URI (see app_logo_src)
        # because ft.app() here accepts no assets_dir to serve the file from.
        # If the asset cannot be read, fall back to a drawn tile -- a broken
        # image box is worse than a close copy.
        logo_src = app_logo_src()
        if logo_src is not None:
            app_logo = ft.Image(
                src=logo_src, width=20, height=20,
                fit=ft.BoxFit.CONTAIN,
            )
        else:
            # Rounded, not a hard square, and a send mark rather than a generic
            # download arrow, so the fallback is not a visibly different icon.
            # Colours are theme roles, not literals: the fallback must not be
            # the one place a hardcoded scheme sneaks back in.
            app_logo = ft.Container(
                width=20, height=20,
                border_radius=ui.R_SM,
                bgcolor=ft.Colors.PRIMARY,
                alignment=ft.Alignment.CENTER,
                content=ft.Icon(ft.Icons.SEND_ROUNDED,
                                color=ft.Colors.ON_PRIMARY, size=13),
            )

        wordmark_txt = ui.text("Telegram Downloader", role=ui.ROLE_BODY,
                               weight=ui.W_SEMI)
        brand = ui.cluster(
            [app_logo, wordmark_txt],
            spacing=ui.S2)
        divider = ft.Container(content=ui.rule(vertical=True), height=18)

        # -- Body container ------------------------------------------------
        # The views own no scrolling of their own; the body is the one region
        # that is inset and centred, and each view decides whether it scrolls.
        # `expand` matters on the container: without it the box sizes itself to
        # its content and everything below the fold becomes unreachable.
        body_content = ft.Container(expand=True)
        body = ft.Container(
            content=body_content,
            expand=True,
            alignment=ft.Alignment.TOP_CENTER,
            padding=ft.Padding(ui.PAGE_PAD, ui.S4, ui.PAGE_PAD, ui.S4),
        )

        # deps_view used to be a fourth tab. It now lives inside Settings as a
        # section (deps_section), so the top tabs are Add / Queue / Settings.
        views = [downloads_view(page), queue_view(page), auth_view(page), settings_view(page)]

        # -- Top navigation ---------------------------------------------------
        # Chips built from ui.nav_tab primitives on purpose. flet 0.86.5's tab
        # controls do not fit: ft.Tabs owns its own content and its tabs are
        # positional children, so _goto() could not drive selection the way
        # refresh_queue() and the first-run nudge need; ft.TabBar is header-only
        # but has neither selected_index nor on_change. A row of chips keeps the
        # content ours and gives us the selected state to style.
        _tab_chips: list = []
        _chip_pad: dict = {}   # idx -> the chip's labelled padding

        def _keyed(control, data):
            # ui.nav_tab carries its own identity (data=label). These chips are
            # keyed by INDEX instead, because that is what they index into
            # `views`, so the label identity is overridden here.
            control.data = data
            return control

        def _make_chip(idx: int, label: str, icon):
            """One tab. Selected is a RAISED PILL, not an accent underline.

            Colour in this app means action or status; "where am I" is neither,
            so the tab you are on is a lighter surface plus a hairline -- the
            same material as the selected segment of the output-mode control.
            """
            def _pick(e):
                _goto(idx)
            tab = _keyed(ui.nav_tab(label, selected=False, leading=icon,
                                    on_click=_pick), data=idx,)
            _tab_chips.append(tab)
            # The labelled padding belongs to the chip, so keep it: the
            # compact form puts it back instead of hardcoding a second copy.
            _chip_pad[idx] = tab.padding
            return tab

        # queue_chip is built FIRST (refresh_queue needs a handle on it to
        # badge it) but renders SECOND, so construction order is
        # [Ongoing, Add, Account auth, Settings] while the screen shows
        # [Add, Ongoing, Account auth, Settings].
        # That is exactly why selection matches on chip.data.
        ongoing_chip = _make_chip(1, "Ongoing", ft.Icons.PENDING_ACTIONS)
        _add_chip = _make_chip(0, "Add", ft.Icons.LINK_OUTLINED)
        auth_chip = _make_chip(2, "Account auth", ft.Icons.PERSON_OUTLINED)
        _settings_chip = _make_chip(3, "Settings", ft.Icons.SETTINGS_OUTLINED)

        # -- The bar's widths -------------------------------------------------
        # The strip has to survive a 360px phone, and it has four
        # destinations, a brand and a theme toggle in it, so something has
        # to give. It gives in this order, measured rather than guessed:
        #
        #   the wordmark   the four labelled chips need ~530px on their own
        #                   and the wordmark another ~145, so the app's name
        #                   only appears once the window can pay for it.
        #   the labels     below that the strip is an ICON RAIL: four square
        #                   icon buttons that fit in 360px with room to
        #                   spare. The name is not lost -- it moves into
        #                   the chip's tooltip -- and the page you are on is
        #                   still the raised pill, so the strip still says
        #                   where you are.
        #
        # The wordmark goes before the labels because the labels ARE the
        # page title in this app (idea 2 in the module docstring): losing
        # the name of the page you are on costs more than losing the name
        # of the app.
        LABELS_MIN_WIDTH = 580
        WORDMARK_MIN_WIDTH = 700
        ICON_CHIP = 30          # the icon-only chip's box, in compact mode
        # A row of fixed-width action buttons needs about 400px of page; a
        # phone gets the column arrangement instead.
        ACTIONS_MIN_WIDTH = 480

        def _set_chip_labelled(chip, labelled: bool):
            """Show a chip's label, or drop it and keep the icon.

            The label is the chip's Text; hiding it leaves the icon, and the
            icon is the chip's identity at that size -- the tooltip keeps the
            name one hover away.
            """
            for part in chip.content.controls:
                if isinstance(part, ft.Text):
                    part.visible = labelled
                    chip.tooltip = None if labelled else part.value
            if labelled:
                chip.width = None
                chip.height = None
                chip.alignment = None
                chip.padding = _chip_pad[chip.data]
            else:
                # A square icon button, not a squeezed pill: the icon sits
                # in the middle of a fixed box, so the four chips stay the
                # same size whether or not they are the selected one.
                chip.width = ICON_CHIP
                chip.height = ICON_CHIP
                chip.alignment = ft.Alignment.CENTER
                chip.padding = ft.Padding(0, 0, 0, 0)

        # -- Responsive handler -------------------------------------------------
        # Inside _main so it closes over the bar's parts. `queue_toolbar_summary_ref`
        # comes from run_gui scope.
        COMPACT_WIDTH = 560  # Breakpoint for compact/mobile layout

        def _apply_responsive(e):
            """Lay the top bar out for the width it is actually at.

            The bar sheds its wordmark first and its chip labels second (see
            LABELS_MIN_WIDTH above). Below the label threshold the strip
            becomes an icon rail, which is the only way four destinations, a
            brand and a theme toggle all fit on a 360px phone.

            Flet calls this with the PAGE at startup and with a
            PageResizeEvent on every resize, so the width is read from
            whichever of the two we were handed. The event is not the page:
            calling page.update() on it raised
            "'PageResizeEvent' object has no attribute 'update'" and took
            the whole app down the first time a window was dragged narrow,
            which is exactly the case this handler exists for.
            """
            width = getattr(e, "width", None) or page.width or 0
            labelled = width >= LABELS_MIN_WIDTH
            wordmark_txt.visible = width >= WORDMARK_MIN_WIDTH
            # The hairline marks where the brand ends and the navigation
            # begins. With no labels there is nothing for it to separate, and
            # at 360px every pixel of the strip is spoken for, so it goes.
            divider.visible = labelled
            for chip in _tab_chips:
                _set_chip_labelled(chip, labelled)
            # Action rows: the row arrangement does not fit a phone, so the
            # page bodies that have both show the column one instead.
            stacked = width < ACTIONS_MIN_WIDTH
            for wide, narrow in layout_refs.values():
                wide.visible = not stacked
                narrow.visible = stacked
            # Queue toolbar: hide summary in compact mode, keep buttons
            if queue_toolbar_summary_ref is not None:
                queue_toolbar_summary_ref.visible = width > COMPACT_WIDTH
            page.update()

        # ONE top bar: brand, the two primary destinations, the theme toggle,
        # then the two settings-side destinations.
        #   [◆ Telegram Downloader]  │  Add   Ongoing   ·····  [☀]  Account auth   Settings
        # The theme toggle is chrome, not a destination, so it gets a little
        # air on its right and never reads as another tab.
        theme_slot = ft.Container(content=theme_btn,
                                  margin=ft.Margin(0, 0, ui.S2, 0))
        top_bar = ui.bar(
            [brand, divider, _add_chip, ongoing_chip,
             ui.spacer(), theme_slot, auth_chip, _settings_chip],
            rule_below=True)

        # Expose the Ongoing chip so refresh_queue can put the pending count on it.
        queue_header_txt["tab"] = ongoing_chip
        # Which view is on screen. Guards re-taps of the active chip.
        _current_idx = -1

        def _set_selected_index(idx: int):
            # Selection is an accent-free raised pill: fill plus hairline on
            # the chip that is on screen, nothing at all on the others.
            #
            # Matched on chip.data, never on position -- ongoing_chip is built
            # before the others, so list order is not visual order.
            for chip in _tab_chips:
                on = (chip.data == idx)
                chip.bgcolor = (ft.Colors.SURFACE_CONTAINER_HIGH if on
                                else None)
                chip.border = (ft.Border(
                    top=ft.BorderSide(1, ui.EDGE),
                    left=ft.BorderSide(1, ui.HAIRLINE),
                    right=ft.BorderSide(1, ui.HAIRLINE),
                    bottom=ft.BorderSide(1, ui.HAIRLINE),
                ) if on else None)
                # ui.nav_tab colours its icon and its label separately, so the
                # selected state has to be repainted part by part.
                for part in chip.content.controls:
                    part.color = (ft.Colors.ON_SURFACE if on
                                  else ft.Colors.ON_SURFACE_VARIANT)
                    if isinstance(part, ft.Text):
                        part.weight = ui.W_SEMI if on else ui.W_MEDIUM

        def on_nav(idx: int):
            # `nonlocal` is required, not decorative. Assigning _current_idx
            # here makes Python treat it as a LOCAL of on_nav, so the read on
            # the guard line below raised UnboundLocalError ("cannot access
            # local variable '_current_idx' where it is not associated with a
            # value") the first time any tab was clicked.
            nonlocal _current_idx
            # Re-tapping the active tab used to rebuild the view (Queue
            # re-rendered every row) and flash. No-op instead.
            if idx == _current_idx:
                return
            _current_idx = idx
            _set_selected_index(idx)
            body_content.content = views[idx]
            # ONE update per switch. refresh_queue()/refresh_mode_hint()
            # each end with their own page.update(), so updating here as well
            # serialised the entire control tree twice per click.
            if idx == 1:
                refresh_queue()
            elif idx == 0:
                # The output-mode control lives on the Add page and is built
                # once, so a mode changed elsewhere (a reset) would leave it
                # showing the old choice. Repaint it on the way in.
                refresh_mode_hint()
                try:
                    page.update()
                except Exception:
                    pass
            else:
                try:
                    page.update()
                except Exception:
                    pass

        def _goto(idx: int):
            on_nav(idx)

        nav["goto"] = _goto

        # -- Bottom bar: download location ---------------------------------
        # Pinned to the bottom of the window so the folder can be changed in
        # one click. It was previously editable only inside Settings, so
        # changing where downloads land meant navigating there first -- and the
        # field most likely to need changing was the one furthest from view.
        dl_path_txt = ui.text("", role=ui.ROLE_META, no_wrap=True,
                              overflow=ft.TextOverflow.ELLIPSIS)

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
            if await pick_download_dir(page):
                _refresh_dl_label()
                try:
                    page.update()
                except Exception:
                    pass

        # A raised strip, not a card and not a full-bleed band: the path is the
        # persistent object the user acts on, and it reads as chrome above the
        # content rather than as another row in it.
        dl_bar = ui.bar(
            [ft.Icon(ft.Icons.FOLDER_OUTLINED, size=14,
                     color=ft.Colors.ON_SURFACE_VARIANT),
             _grow(dl_path_txt, 1),
             # Reveal the folder in the desktop file manager. Next to the path
             # it actually refers to, it costs one 30px icon button.
             ui.icon_action(ft.Icons.OPEN_IN_NEW, "Open download folder",
                            lambda e: open_folder(page)),
             ui.action("Change", on_click=_pick_download_dir,
                       kind="secondary")],
            tone="raised", pad_y=ui.S2)
        dl_bar.border = ft.Border(top=ft.BorderSide(1, ui.EDGE))
        _refresh_dl_label()

        # -- Page shell: top bar, body, footer ------------------------------
        # No page.appbar: the brand, the tabs and the theme toggle are all rows
        # inside the top bar, so there is one header instead of two.
        # First-run nudge: open Settings when API credentials or the download
        # folder are missing, because nothing can work without them.
        st0 = load_settings()
        start_idx = 2 if (not st0.api_id or not st0.api_hash
                          or not st0.download_dir) else 0
        _set_selected_index(start_idx)
        body_content.content = views[start_idx]

        page.add(ft.Column(
            [
                top_bar,
                body,
                dl_bar,
            ],
            expand=True,
            spacing=0,
        ))

        # -- Responsive: re-layout on window resize (phone/tablet/desktop) ---
        # Without this the top bar's wordmark and the queue toolbar's
        # summary text are laid out for the width the app first saw, so dragging
        # a window from 1280px to 390px left the Settings tab and the theme
        # toggle clipped off the right edge. `page.on_resize` fires once per
        # resize event, so a drag animates smoothly.
        page.on_resize = _apply_responsive

        # Apply once at startup: page.width is already the real width here, so
        # the first frame is correct at every width, not just the one the window
        # happened to be opened at.
        _apply_responsive(page)

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
                    "Settings has the same list and a Re-check button.")
            go = await confirm_dialog(
                page, "Missing dependencies", body,
                action="Open Settings", icon=ft.Icons.WARNING_AMBER)
            if go:
                _goto(2)

        page.run_task(_first_run_dep_check)

    # ------------------------------------------------------------ entry
    # `view` exists so the GUI can be served over HTTP on a headless machine
    # (Codespaces, CI, a container): without it flet opens a desktop window and
    # dies when there is no display. Left None, behaviour is unchanged.
    if view is not None:
        ft.app(target=_main, port=port, view=view)
    elif port:
        ft.app(target=_main, port=port)
    else:
        ft.app(target=_main)
