"""Flet desktop/mobile GUI — Material3 design matching simple-yt-downloader.

Dark-first purple seed (#6750A4), header blocks (tinted icon + bold title
+ subtitle), section cards (20px padding, icon + title + optional subtitle),
info rows (primary icon + label/value), state-colored queue rows with
% pills, progress bars, byte details and error containers.

Pages: Add (URLs -> fetch titles -> confirm -> download), Queue,
Settings (connection/defaults/about + update checker), Dependencies.

Run:  tg-dl gui   (needs `pip install -e '.[gui]'`)
"""
from __future__ import annotations

import asyncio
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

SEED = "#6750A4"
_queue_widgets = {}  # item_id -> {card, progress_bar, pct_pill, detail_txt, ...}
MODE_LABELS = {"1": "Original", "2": "Fast", "3": "Max"}


def run_gui(port=None):
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
                    try:
                        st0 = load_settings()
                        st0.user_id = None
                        save_settings(st0)
                    except Exception:
                        pass
                    raise RuntimeError(
                        "You seem to have switched Telegram accounts. "
                        "For safety, you've been signed out. Please log in again.")
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
            sb.content = ft.Text(msg)
            sb.bgcolor = ft.Colors.ERROR_CONTAINER if error else ft.Colors.PRIMARY_CONTAINER
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
            padding=20))

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
            ft.Text(text, size=12, weight=ft.FontWeight.BOLD,
                    color=ft.Colors.ON_PRIMARY_CONTAINER),
            bgcolor=ft.Colors.PRIMARY_CONTAINER,
            border_radius=8, padding=ft.padding.symmetric(horizontal=12, vertical=4))

    def status_label(status: str):
        """Small theme-aware status text for non-active queue rows."""
        mapping = {
            "queued": ("Queued — waiting to start", ft.Colors.ON_SURFACE_VARIANT),
            "fetching": ("Fetching…", ft.Colors.PRIMARY),
            "done": ("Saved", ft.Colors.TERTIARY),
            "error": ("Failed — see details below", ft.Colors.ERROR),
            "cancelled": ("Cancelled — partial kept for resume",
                           ft.Colors.ON_SURFACE_VARIANT),
        }
        text, color = mapping.get(status, (status, ft.Colors.ON_SURFACE_VARIANT))
        return ft.Text(text, size=12, color=color)

    def error_box(message: str):
        return ft.Container(
            ft.Row([ft.Icon(ft.Icons.ERROR, size=16,
                            color=ft.Colors.ON_ERROR_CONTAINER),
                    ft.Text(message, size=12, expand=True, selectable=True,
                            color=ft.Colors.ON_ERROR_CONTAINER)],
                   spacing=8),
            bgcolor=ft.Colors.ERROR_CONTAINER, border_radius=8, padding=12)

    def hint_box(message: str):
        """Neutral hint line: icon + wrapped secondary text."""
        return ft.Row([
            ft.Icon(ft.Icons.INFO, size=16, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(message, size=12, expand=True,
                    color=ft.Colors.ON_SURFACE_VARIANT),
        ], spacing=8)

    def empty_state(icon, title: str, subtitle: str,
                    action_label: str | None = None,
                    action_icon=None, on_action=None):
        controls = [
            ft.Icon(icon, size=48, color=ft.Colors.PRIMARY),
            ft.Text(title, size=20, weight=ft.FontWeight.W_600),
            ft.Text(subtitle, size=14, color=ft.Colors.ON_SURFACE_VARIANT),
        ]
        if action_label and on_action is not None:
            controls.append(ft.FilledTonalButton(
                action_label, icon=action_icon, on_click=on_action))
        return ft.Container(
            ft.Column(controls, spacing=8,
                      alignment=ft.MainAxisAlignment.CENTER,
                      horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            padding=40)

    def state_icon(status: str):
        mapping = {
            "queued": (ft.Icons.SCHEDULE, ft.Colors.ON_SURFACE_VARIANT),
            "fetching": (ft.Icons.SYNC, ft.Colors.PRIMARY),
            "downloading": (ft.Icons.DOWNLOAD, ft.Colors.PRIMARY),
            "converting": (ft.Icons.SYNC, ft.Colors.SECONDARY),
            "done": (ft.Icons.CHECK_CIRCLE, ft.Colors.TERTIARY),
            "error": (ft.Icons.ERROR, ft.Colors.ERROR),
            "cancelled": (ft.Icons.CANCEL, ft.Colors.ON_SURFACE_VARIANT),
        }
        icon, color = mapping.get(status, (ft.Icons.SCHEDULE, ft.Colors.ON_SURFACE_VARIANT))
        return ft.Icon(icon, size=24, color=color)

    # ------------------------------------------------------------ ADD page
    def downloads_view(page: ft.Page):
        url_count_txt = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        def _on_url_change(e):
            text = url_box.value or ""
            urls = _re_module.findall(r'https?://t\.me/\S+', text)
            n = len(urls)
            url_count_txt.value = f"{n} URL{'s' if n != 1 else ''} detected" if n else ""
            url_box.helper_text = (url_count_txt.value
                                   if n else "Nothing downloads until you confirm below.")

        url_box = ft.TextField(label="Telegram link(s)",
                               hint_text="https://t.me/channel/123 (one per line)",
                               helper_text="Nothing downloads until you confirm below.",
                               prefix_icon=ft.Icons.LINK,
                               multiline=True, min_lines=3, max_lines=6, expand=True,
                               text_size=16, content_padding=ft.padding.all(12),
                               on_change=_on_url_change)
        preview_list = ft.ListView(spacing=8, expand=True)
        status_txt = ft.Text("", size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                             expand=True)
        count_txt = ft.Text("No previews yet — paste links and tap Fetch titles.",
                            size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        selected_txt = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                               expand=True)
        fetch_btn = ft.FilledButton("Fetch titles", icon=ft.Icons.SEARCH, height=40)
        dl_btn = ft.FilledButton("Download selected", icon=ft.Icons.DOWNLOAD,
                                 disabled=True, height=40)
        fetch_label = ft.Text("Fetching titles...", size=12,
                              color=ft.Colors.PRIMARY, visible=False)
        bar = ft.ProgressBar(visible=False, bar_height=6)
        fetch_row = ft.Row([fetch_label, bar], spacing=8, visible=False)
        mode_seg = ft.SegmentedButton(
            segments=[ft.Segment(value="1", label=ft.Text("Original"),
                                 icon=ft.Icons.FILE_DOWNLOAD,
                                 tooltip="Original: raw file, fastest, no conversion"),
                      ft.Segment(value="2", label=ft.Text("Fast"),
                                 icon=ft.Icons.BOLT,
                                 tooltip="Fast: compressed MP4, veryfast preset, smaller file"),
                      ft.Segment(value="3", label=ft.Text("Max"),
                                 icon=ft.Icons.HD,
                                 tooltip="Max: best quality compressed, medium speed, smallest file")],
            selected=[state["mode"] if state["mode"] in MODE_LABELS else "1"])
        state["mode_seg"] = mode_seg

        def _on_mode(e):
            sel = e.control.selected
            if sel:
                state["mode"] = next(iter(sel))
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
                    ft.Icons.SEARCH, "No previews yet",
                    "Paste links above and tap Fetch titles."))
                count_txt.value = "No previews yet — paste links and tap Fetch titles."
            else:
                count_txt.value = (f"{len(oks)} downloadable"
                                   + (f" • {bad} skipped" if bad else ""))
            n_sel = sum(1 for r in oks if r["selected"])
            selected_txt.value = (f"{n_sel} of {len(oks)} selected."
                                  if oks else "")
            dl_btn.disabled = n_sel == 0
            for i, r in enumerate(state["previews"]):
                if r["ok"]:
                    title_field = ft.TextField(
                        value=r["title"], label=f"Title {i + 1}", dense=True, expand=True,
                        on_change=lambda e, idx=i: state["previews"].__getitem__(idx).__setitem__("title", e.control.value))
                    chk = ft.Checkbox(
                        value=r["selected"],
                        tooltip="Include in download",
                        on_change=lambda e, idx=i: (
                            state["previews"].__getitem__(idx).__setitem__(
                                "selected", e.control.value),
                            render_previews()))
                    preview_list.controls.append(ft.Card(ft.Container(ft.Row([
                        chk,
                        ft.Column([title_field,
                                   ft.Text(f"{r['ext']}  •  {format_bytes(r['size'])}",
                                           size=12, color=ft.Colors.ON_SURFACE_VARIANT)],
                                  spacing=2, expand=True),
                    ], spacing=12), padding=16)))
                else:
                    preview_list.controls.append(error_box(f"{_sanitize_display(r['url'])}\nSkipped: {_sanitize_display(r['error'])}"))
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
            fp = ft.FilePicker()
            page.overlay.append(fp)
            page.update()
            try:
                res = await fp.pick_files(
                    dialog_title="Choose URL list",
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["txt"], allow_multiple=False)
            except Exception as exc:
                snack(page, f"Cannot open file picker: {exc}", error=True)
                return
            try:
                files = getattr(res, "files", None) or []
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
            finally:
                # Flet picker instances stay registered on the overlay if left
                # behind; remove it so repeated picks don't grow the overlay.
                try:
                    page.overlay.remove(fp)
                    page.update()
                except Exception:
                    pass

        def on_clear(e):
            url_box.value = ""
            state["previews"] = []
            preview_list.controls.clear()
            preview_list.controls.append(empty_state(
                ft.Icons.SEARCH, "No previews yet",
                "Paste links above and tap Fetch titles."))
            count_txt.value = "No previews yet — paste links and tap Fetch titles."
            selected_txt.value = ""
            dl_btn.disabled = True
            status_txt.value = ""
            page.update()

        fetch_btn.on_click = on_fetch
        dl_btn.on_click = on_download
        render_previews()
        return ft.Column([
            h1(ft.Icons.DOWNLOAD, "Telegram Downloader",
               "Paste t.me links, fetch titles, then queue downloads."),
            section("Add Download", [
                url_box,
                ft.Row([ft.Text("Mode:", size=14), mode_seg], spacing=16),
                ft.Text("Original = raw file (fastest). Fast = compressed MP4 (veryfast, smaller). Max = best quality compressed (medium, smallest).",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Row([fetch_btn,
                        ft.OutlinedButton("Load .txt", icon=ft.Icons.FILE_OPEN,
                                          on_click=on_load_file),
                        ft.TextButton("Clear", icon=ft.Icons.CLEAR,
                                      on_click=on_clear)], spacing=12, wrap=True),
                fetch_row,
                ft.Row([ft.Icon(ft.Icons.INFO, size=16,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        status_txt], spacing=8),
            ], icon=ft.Icons.LINK,
                subtitle="One link per line — titles are fetched before anything downloads."),
            section("Preview — confirm before download", [
                ft.Row([count_txt,
                        ft.TextButton("All", on_click=on_select_all),
                        ft.TextButton("None", on_click=on_select_none)],
                       spacing=4),
                preview_list,
                ft.Row([selected_txt, dl_btn], spacing=12, wrap=True),
            ], icon=ft.Icons.PLAYLIST_ADD_CHECK,
                subtitle="Tick + edit titles, then confirm."),
            section("Quick Actions", [
                ft.Row([
                    ft.OutlinedButton("Open folder", icon=ft.Icons.FOLDER_OPEN,
                                      on_click=lambda e: open_folder(page)),
                    ft.OutlinedButton("Settings", icon=ft.Icons.SETTINGS,
                                      on_click=lambda e: nav["goto"](2) if "goto" in nav else None),
                ], spacing=12, run_spacing=12, wrap=True),
            ], icon=ft.Icons.BOLT,
                subtitle="Common destinations and checks."),
        ], spacing=20, scroll=ft.ScrollMode.AUTO, expand=True)

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
                ctrl.spans = [
                    ft.TextSpan(f"{n_active} active",
                                ft.TextStyle(color=ft.Colors.PRIMARY)),
                    ft.TextSpan("  "),
                    ft.TextSpan(f"{n_queued} queued",
                                ft.TextStyle(color=ft.Colors.ON_SURFACE_VARIANT)),
                    ft.TextSpan("  "),
                    ft.TextSpan(f"{n_done} done",
                                ft.TextStyle(color=ft.Colors.TERTIARY)),
                    ft.TextSpan("  "),
                    ft.TextSpan(f"{n_failed} need attention",
                                ft.TextStyle(color=ft.Colors.ERROR)),
                ]
        # Update Queue navigation badge with active + error count.
        badge_count = n_active + n_failed
        for key in ("rail_dest", "bar_dest"):
            dest = queue_header_txt.get(key)
            if dest is not None:
                try:
                    dest.badge = ft.Badge(str(badge_count),
                                          bg_color=ft.Colors.ERROR) if badge_count > 0 else None
                except Exception:
                    pass
        
        # Remove widgets for items no longer in queue
        current_ids = {it.id for it in items}
        for old_id in list(_queue_widgets.keys()):
            if old_id not in current_ids:
                del _queue_widgets[old_id]
        
        if not items:
            if not queue_list.controls or not isinstance(queue_list.controls[0], ft.Card):
                queue_list.controls.clear()
                queue_list.controls.append(empty_state(
                    ft.Icons.INBOX, "No downloads in queue",
                    "Add links from the Add tab to get started.",
                    action_label="Add downloads", action_icon=ft.Icons.ADD,
                    on_action=lambda e: nav["goto"](0) if "goto" in nav else None))
            return
        
        # Remove empty state if present
        if queue_list.controls and not hasattr(queue_list.controls[0], 'data'):
            queue_list.controls.clear()
        
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
                    widgets['pct_pill'].content.value = f"{pct:.1f}%"
                # Update detail text
                if widgets.get('detail_txt'):
                    speed = getattr(it, "download_speed", 0) or 0
                    speed_txt = f"  •  {format_bytes(speed)}/s" if speed > 0 else ""
                    detail = (f"{format_bytes(it.current_bytes)}/{format_bytes(it.total_bytes or it.size)}"
                              + speed_txt
                              + ("  •  converting…" if it.status == "converting"
                                 else "  •  downloading…"))
                    widgets['detail_txt'].value = detail
                # Add existing card to controls
                new_controls.append(widgets['card'])
            else:
                # Create new widgets for this item
                subtitle_bits = [b for b in
                                 [it.ext or "", format_bytes(it.total_bytes or it.size)] if b]
                actions = []
                if it.status in ("error", "cancelled"):
                    actions.append(ft.IconButton(ft.Icons.REFRESH, tooltip="Retry",
                                                 icon_color=ft.Colors.PRIMARY,
                                                 on_click=lambda e, _id=it.id: (
                                                     store.update(_id, status="queued", error=""),
                                                     start_soon(page))))
                if it.status in ("downloading", "converting", "fetching"):
                    actions.append(ft.IconButton(ft.Icons.STOP, tooltip="Stop current download\n(rest stay queued)",
                                                 icon_color=ft.Colors.ERROR,
                                                 on_click=lambda e, _id=it.id: cancel_id(
                                                     page, _id)))
                if it.status not in ("downloading", "converting", "fetching"):
                    actions.append(ft.IconButton(ft.Icons.CLOSE, tooltip="Remove",
                                                 icon_color=ft.Colors.ON_SURFACE_VARIANT,
                                                 on_click=lambda e, _id=it.id: (
                                                     store.remove(_id), refresh_queue())))
                title_txt = _sanitize_display(f"{it.title}{it.ext}", limit=120)
                title_col: list = [ft.Text(title_txt, size=15, weight=ft.FontWeight.W_500,
                                           max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)]
                if subtitle_bits:
                    title_col.append(ft.Text("  •  ".join(subtitle_bits), size=12,
                                             color=ft.Colors.ON_SURFACE_VARIANT))
                
                # Create mutable widgets we'll update later
                pct_pill_widget = pct_pill(f"{pct:.1f}%") if it.status in (
                    "downloading", "converting") else ft.Container()
                
                head_row = ft.Row([state_icon(it.status),
                            ft.Column(title_col, spacing=2, expand=True),
                            pct_pill_widget,
                            *actions], spacing=12)
                card_body: list = [head_row]
                
                # Plain-language status line for idle states (queued/done/cancelled).
                if it.status in ("queued", "cancelled"):
                    card_body.append(status_label(it.status))
                
                progress_bar = None
                detail_txt = None
                if it.status in ("downloading", "converting"):
                    speed = getattr(it, "download_speed", 0) or 0
                    speed_txt = f"  •  {format_bytes(speed)}/s" if speed > 0 else ""
                    detail = (f"{format_bytes(it.current_bytes)}/{format_bytes(it.total_bytes or it.size)}"
                              + speed_txt
                              + ("  •  converting…" if it.status == "converting"
                                 else "  •  downloading…"))
                    progress_bar = ft.ProgressBar(value=pct / 100, bar_height=6)
                    detail_txt = ft.Text(detail, size=12, color=ft.Colors.ON_SURFACE_VARIANT)
                    card_body += [progress_bar, detail_txt]
                
                if it.status == "error" and it.error:
                    card_body.append(error_box(_sanitize_display(it.error)))
                if it.status == "done":
                    if it.dest:
                        card_body.append(ft.Row([
                            ft.Icon(ft.Icons.CHECK_CIRCLE, size=16,
                                    color=ft.Colors.TERTIARY),
                            ft.Text(it.dest, size=12, expand=True,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ], spacing=8))
                    elif it.error:
                        # Kept-original note (e.g. ffmpeg missing) surfaces here.
                        card_body.append(hint_box(it.error))
                
                card = ft.Card(ft.Container(ft.Column(card_body, spacing=8), padding=16))
                new_controls.append(card)
                
                # Store references to mutable widgets
                _queue_widgets[it.id] = {
                    'card': card,
                    'progress_bar': progress_bar,
                    'pct_pill': pct_pill_widget if hasattr(pct_pill_widget, 'content') else None,
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

    def queue_view(page: ft.Page):
        import flet as ft
        nonlocal queue_list
        queue_list = ft.ListView(spacing=8, expand=True)
        header = ft.Text(f"Downloads ({len(store.items)})", size=16,
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

        toolbar = ft.Card(ft.Container(ft.Column([
            ft.Row([header, ft.Container(expand=True),
                    ft.TextButton("Clear finished", icon=ft.Icons.DELETE,
                                  on_click=lambda e: (store.clear_finished(), refresh_queue()))],
                   spacing=8),
            ft.Divider(height=1),
            summary,
            ft.Row([ft.FilledTonalButton("Resume all / Start", icon=ft.Icons.PLAY_ARROW,
                                         on_click=on_resume_all),
                    ft.OutlinedButton("Cancel batch", icon=ft.Icons.STOP,
                                      on_click=on_cancel)], spacing=12, wrap=True),
        ], spacing=12), padding=16))
        refresh_queue()
        return ft.Column([
            h1(ft.Icons.QUEUE, "Queue",
               "Track progress, retry errors, clear finished downloads."),
            toolbar,
            queue_list,
        ], spacing=20, expand=True)

    # ------------------------------------------------------------ settings
    settings_refs: dict = {}

    def refresh_settings_hint():
        try:
            st = load_settings()
            if settings_refs.get("mode_txt") is not None:
                settings_refs["mode_txt"].value = MODE_LABELS.get(st.output_mode, st.output_mode)
            if settings_refs.get("folder_txt") is not None:
                settings_refs["folder_txt"].value = st.download_dir or str(default_download_dir())
            if settings_refs.get("theme_sw") is not None:
                settings_refs["theme_sw"].value = (st.theme or "dark") != "light"
        except Exception:
            pass

    def settings_view(page: ft.Page):
        import flet as ft
        st = load_settings()
        api_id_f = ft.TextField(label="API ID",
                                helper_text="Get from my.telegram.org → API development tools",
                                value=st.api_id or "", prefix_icon=ft.Icons.API)
        api_hash_f = ft.TextField(label="API hash (keep private)", value=st.api_hash or "",
                                  password=True, can_reveal_password=True,
                                  prefix_icon=ft.Icons.PASSWORD)
        dl_f = ft.TextField(label="Download folder",
                            value=st.download_dir or str(default_download_dir()),
                            prefix_icon=ft.Icons.FOLDER, expand=True)
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
        theme_sw = ft.Switch(value=(st.theme or "dark") != "light")
        settings_refs.update(mode_txt=mode_txt, folder_txt=folder_txt, theme_sw=theme_sw)
        settings_error = ft.Container(visible=False)

        async def pick_dir(e):
            fp = ft.FilePicker()
            page.overlay.append(fp)
            page.update()
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
            s.download_dir = dl_f.value.strip()
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

        def on_theme(e):
            s = load_settings()
            s.theme = "light" if not e.control.value else "dark"
            save_settings(s)
            apply_theme(page, s.theme)

        theme_sw.on_change = on_theme

        check_updates_btn = ft.FilledTonalButton("Check for Updates", icon=ft.Icons.UPDATE,
                                                  on_click=on_check_updates)

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

        async def download_installer(page, info):
            asset = info.asset_for_platform()
            if asset is None:
                snack(page, "No installer for your OS — opening release page instead.", error=True)
                try:
                    updates.open_release_page(info.release_url)
                except Exception:
                    pass
                return
            snack(page, f"Downloading {asset.name}...")
            try:
                import os as _os
                token = _os.environ.get("GITHUB_TOKEN") or ""
                dest = await asyncio.to_thread(
                    updates.download_asset, asset,
                    ensure_download_dir(load_settings()), info.latest_version,
                    token or None)
                snack(page, f"Saved installer to {dest}. Install it manually to upgrade.")
            except Exception as exc:
                snack(page, f"Download failed: {exc}", error=True)

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

        first = not st.api_id or not st.api_hash or not st.download_dir
        conn_body: list = []
        if first:
            conn_body.append(hint_box(
                "API credentials are required once, then stored privately (0600)."))
        conn_body += [
            api_id_f, api_hash_f,
            ft.Row([dl_f, ft.OutlinedButton("Browse", icon=ft.Icons.FOLDER_OPEN,
                                            on_click=pick_dir)], spacing=12),
            folder_error,
            settings_error,
            ft.Row([ft.FilledButton("Save", icon=ft.Icons.SAVE, on_click=on_save)],
                   spacing=12, wrap=True),
            ft.Row([ft.FilledTonalButton("Login / verify", icon=ft.Icons.LOGIN,
                                          on_click=on_login),
                    ft.OutlinedButton("Logout", icon=ft.Icons.LOGOUT,
                                      on_click=on_logout)], spacing=12, wrap=True),
        ]
        return ft.Column([
            h1(ft.Icons.SETTINGS, "Settings",
               "Credentials, folders, appearance and updates."),
            section("Connection", conn_body, icon=ft.Icons.KEY,
                    subtitle="Asked once, stored privately (0600)."),
            section("Download Defaults", [
                info_row_ctrl(ft.Icons.FOLDER, "Output Directory", folder_txt),
                info_row_ctrl(ft.Icons.DOWNLOAD, "Output Mode", ft.Row([
                    mode_txt,
                    ft.Text("Change it on the Add tab.", size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                ], spacing=8)),
            ], icon=ft.Icons.FOLDER,
                subtitle="Used for new downloads."),
            section("Appearance", [
                ft.Row([ft.Icon(ft.Icons.DARK_MODE, size=20,
                                color=ft.Colors.PRIMARY),
                        ft.Column([ft.Text("Dark Theme", size=14),
                                   ft.Text("Use dark theme.", size=12,
                                           color=ft.Colors.ON_SURFACE_VARIANT)],
                                  spacing=2, expand=True),
                        theme_sw], spacing=12),
            ], icon=ft.Icons.DARK_MODE,
                subtitle="Dark-first, like the reference app."),
            section("About", [
                *_about_rows(),
                ft.Row([check_updates_btn],
                       alignment=ft.MainAxisAlignment.START),
            ], icon=ft.Icons.INFO,
                subtitle="Version, engine and updater."),
            ft.Row([ft.OutlinedButton("Reset to Defaults", icon=ft.Icons.RESTORE,
                                      on_click=on_reset)],
                   alignment=ft.MainAxisAlignment.CENTER),
        ], spacing=20, scroll=ft.ScrollMode.AUTO, expand=True)

    # ------------------------------------------------------------ deps page
    def deps_view(page: ft.Page):
        import flet as ft
        col = ft.Column(spacing=20, scroll=ft.ScrollMode.AUTO, expand=True)
        sys_txt = ft.Text(f"{platform.system()} {platform.release()} • Python {platform.python_version()}",
                          size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        def render(rows_data=None):
            col.controls.clear()
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
                               helper_text="Phone: +1 555... | Bot token: 123456:ABCdef...",
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
        page.theme = ft.Theme(color_scheme_seed=SEED)
        page.dark_theme = ft.Theme(color_scheme_seed=SEED)
        page.padding = 0
        page.window.min_width = 800
        page.window.min_height = 600
        apply_theme(page, load_settings().theme)

        # Responsive body: max-width constraint + adaptive padding
        body_content = ft.Container(expand=True)
        body = ft.Container(
            content=body_content,
            expand=True,
            max_width=1200,
            padding=24,
        )

        views = [downloads_view(page), queue_view(page), settings_view(page)]

        queue_nav_dest = ft.NavigationRailDestination(icon=ft.Icons.QUEUE, label="Queue")
        queue_bar_dest = ft.NavigationBarDestination(icon=ft.Icons.QUEUE, label="Queue")
        nav_destinations = [
            ft.NavigationRailDestination(icon=ft.Icons.LINK, label="Add"),
            queue_nav_dest,
            ft.NavigationRailDestination(icon=ft.Icons.SETTINGS, label="Settings"),
        ]
        bar_destinations = [
            ft.NavigationBarDestination(icon=ft.Icons.LINK, label="Add"),
            queue_bar_dest,
            ft.NavigationBarDestination(icon=ft.Icons.SETTINGS, label="Settings"),
        ]

        rail = ft.NavigationRail(
            selected_index=0, label_type=ft.NavigationRailLabelType.ALL,
            destinations=nav_destinations)

        bar = ft.NavigationBar(
            selected_index=0,
            destinations=bar_destinations)

        # Store nav destinations so refresh_queue can update the badge.
        queue_header_txt["rail_dest"] = queue_nav_dest
        queue_header_txt["bar_dest"] = queue_bar_dest

        def _set_selected_index(idx: int):
            rail.selected_index = idx
            bar.selected_index = idx

        def on_nav(e):
            idx = e.control.selected_index
            _set_selected_index(idx)
            body_content.content = views[idx]
            page.update()
            if idx == 1:
                refresh_queue()
            if idx == 2:
                refresh_settings_hint()

        rail.on_change = on_nav
        bar.on_change = on_nav

        # Responsive layout: rail for wide, bar for narrow
        layout_row = ft.Row([rail, ft.VerticalDivider(width=1), body], expand=True)

        _last_layout_mode = {"mode": None}
        
        def _apply_layout():
            # Determine layout mode
            if page.width < 600:
                mode = "narrow"
                padding = 12
            elif page.width < 900:
                mode = "medium"
                padding = 16
            else:
                mode = "wide"
                padding = 24
            
            # Only update if mode changed or first run
            if _last_layout_mode["mode"] == mode:
                # Just update padding if needed
                if body.padding != padding:
                    body.padding = padding
                return
            
            _last_layout_mode["mode"] = mode
            
            if mode == "narrow":
                # Narrow: hide rail, use bottom bar
                layout_row.controls = [body]
                page.bottom_bar = bar
            else:
                # Wide: use rail, no bottom bar
                layout_row.controls = [rail, ft.VerticalDivider(width=1), body]
                page.bottom_bar = None
            
            body.padding = padding

        def on_resize(e):
            _apply_layout()
            page.update()

        page.on_resize = on_resize

        def _goto(idx: int):
            _set_selected_index(idx)
            body_content.content = views[idx]
            page.update()
            if idx == 1:
                refresh_queue()
            if idx == 2:
                refresh_settings_hint()

        nav["goto"] = _goto
        # First-run nudge: open Settings when API/folder missing.
        st0 = load_settings()
        start_idx = 2 if (not st0.api_id or not st0.api_hash or not st0.download_dir) else 0
        _set_selected_index(start_idx)
        body_content.content = views[start_idx]
        _apply_layout()
        page.add(layout_row)

    import flet as ft
    if port:
        ft.app(target=_main, port=port)
    else:
        ft.app(target=_main)
