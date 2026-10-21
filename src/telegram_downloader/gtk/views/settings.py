"""The Settings destination: dependencies, about, and the danger zone.

Ported from gui.py's settings_view. Dependencies is a SECTION here, not a
tab of its own: configuration and environment belong together, and the
download folder is not here either -- it is changed from the bar along the
bottom of the window, so there is one place to change one value.
"""
from __future__ import annotations

import asyncio
import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from ... import deps as deps_mod
from ... import updates
from ...config import load_settings, save_settings
from ...converter import ffmpeg_version
from ...telegram_utils import format_bytes
from .. import tokens as T
from .. import widgets as w


def app_version() -> str:
    """Never hardcode: read the installed metadata, fall back to the
    in-tree version."""
    try:
        from importlib.metadata import version as _v
        return _v("telegram-downloader")
    except Exception:
        try:
            from .. import __version__ as _dev
            return _dev
        except Exception:
            return "0.0.0-unknown"


class SettingsView:
    """The Settings page."""

    def __init__(self, app):
        self.app = app
        self.engine = app.engine
        # The update download, in flight. A plain dict, written by the
        # download thread and read by the poller on the GTK thread: the
        # worker never touches a widget, and the GTK thread never waits.
        self._update_progress = {"done": 0, "total": 0, "finished": False}
        self._progress_bar = None
        self._progress_detail = None
        self._progress_dialog = None
        self._progress_timer = None
        self._build()

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        page = w.vbox(spacing=T.S4)
        page.add_css_class("page")

        # -- Dependencies
        page.append(w.heading("Dependencies"))
        self.deps_box = w.vbox(spacing=T.S3)
        page.append(self.deps_box)

        # -- About
        page.append(w.heading("About"))
        about = w.vbox(spacing=T.S1)
        about.append(self._key_value("Version", f"v{app_version()}"))
        about.append(self._key_value("Engine", self._engine_version()))
        about.append(self._key_value("FFmpeg", ffmpeg_version() or "Not detected",
                                      mono=True))
        check = w.pending_button("Check for updates", "view-refresh",
                                  self._on_check_updates, kind="secondary",
                                  runner=self.app.run_async)
        # In a row, not straight into the column: a button a column stretches
        # to the full width becomes a slab, and this one is the size of its
        # label in the Flet build.
        check_row = w.hbox(spacing=T.S2)
        check_row.append(check)
        about.append(check_row)
        page.append(about)

        # -- Danger zone
        page.append(w.heading("Danger zone"))
        danger = w.vbox(spacing=T.S3)
        danger.append(w.notice(
            "This signs you out of Telegram, deletes your saved API "
            "credentials, and empties the download queue. Files already "
            "downloaded are not touched. This cannot be undone."))
        reset = w.pending_button(
            "Reset everything", "dialog-warning", self._on_reset,
            kind="danger", runner=self.app.run_async)
        reset_row = w.hbox(spacing=T.S2)
        reset_row.set_halign(Gtk.Align.CENTER)
        reset_row.append(reset)
        danger.append(reset_row)
        page.append(danger)

        self.widget = Gtk.ScrolledWindow()
        self.widget.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.widget.set_child(page)
        self.widget.set_vexpand(True)
        self._render_deps(None)

    def _key_value(self, key: str, value: str, *, mono: bool = False) -> Gtk.Box:
        row = w.hbox(spacing=T.S3)
        k = w.label(key, role="body")
        k.set_hexpand(True)
        row.append(k)
        row.append(w.label(value, role="strong", mono=mono))
        return row

    @staticmethod
    def _engine_version() -> str:
        try:
            import telethon
            return f"Telethon {telethon.__version__}"
        except Exception:
            return "Telethon missing"

    def refresh(self) -> None:
        """Nothing to repaint: the dependency rows are re-read by "Re-check",
        and the About rows are static for the life of the process."""

    # -- dependencies -----------------------------------------------------

    def _render_deps(self, rows_data) -> None:
        """The dependency rows.

        A MISSING row gets its own line with the exact install command and
        a Copy button -- users retype these and get them subtly wrong
        otherwise.
        """
        # Gtk.Box has no get_children(); clear it by removing the children.
        child = self.deps_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.deps_box.remove(child)
            child = nxt

        if rows_data is None:
            rows_data = deps_mod.check_all()
        for d in rows_data:
            row = w.hbox(spacing=T.S3)
            row.add_css_class("row")
            row.append(w.state_dot("done" if d.ok else "failed", size=9))
            middle = w.vbox(spacing=T.S1)
            middle.set_hexpand(True)
            middle.append(w.label(d.name, role="strong"))
            middle.append(w.label(d.hint or "", role="meta"))
            row.append(middle)
            trailing = w.hbox(spacing=T.S2)
            trailing.append(w.label(d.version or "not detected", role="meta",
                                    mono=True))
            trailing.append(w.badge("OK" if d.ok else "MISSING",
                                    tone="ok" if d.ok else "error"))
            row.append(trailing)
            self.deps_box.append(row)
            if not d.ok:
                cmd = d.install_for_current_os()
                line = w.hbox(spacing=T.S2)
                line.append(w.label("$", role="meta", mono=True))
                cmd_label = w.label(cmd, role="meta", mono=True, selectable=True)
                cmd_label.set_hexpand(True)
                line.append(cmd_label)
                line.append(w.button("Copy", kind="quiet", icon="edit-copy",
                                     on_click=lambda _b, c=cmd:
                                     self._copy(c)))
                self.deps_box.append(line)

        recheck = w.pending_button("Re-check", "view-refresh", self._on_recheck,
                                    kind="secondary", runner=self.app.run_async)
        recheck_row = w.hbox(spacing=T.S2)
        recheck_row.append(recheck)
        self.deps_box.append(recheck_row)

    async def _on_recheck(self) -> None:
        # check_all spawns subprocesses (ffprobe -version, etc.): offload to
        # a thread so repeated checks never freeze the UI.
        data = await asyncio.to_thread(deps_mod.check_all)
        self._render_deps(data)

    def _copy(self, text: str) -> None:
        """Copy to the clipboard, falling back to a selection the user can
        copy by hand."""
        try:
            display = Gdk.Display.get_default()
            clipboard = display.get_clipboard()
            clipboard.set(text)
            self.app.toast("Command copied.")
        except Exception:
            # The clipboard is not always there. Show the text instead, so
            # it can still be copied by hand.
            self.app.prompt_async("Copy this command", "", "",
                                  callback=lambda _text: None, value=text)

    # -- about ------------------------------------------------------------

    async def _on_check_updates(self) -> None:
        try:
            info = await asyncio.to_thread(updates.check_for_update)
        except Exception as exc:          # noqa: BLE001 - shown to the user
            self.app.toast(f"Update check failed: {exc}", error=True)
            return
        if info is None:
            self.app.toast("Could not reach the update server.", error=True)
            return
        if not info.is_newer_than(app_version()):
            self.app.toast(f"You are up to date (v{app_version()}).")
            return
        # The dialog is a GTK widget, and this coroutine is on the engine's
        # loop. GTK wants its widgets touched on the main thread, so the
        # dialog is built there.
        self.engine.call_soon(self._update_dialog, info)

    def _update_dialog(self, info) -> None:
        """Later / Open page / Download. Runs on the GTK thread."""
        dialog = Gtk.Dialog(title="Update available", transient_for=self.app.window,
                            modal=True)
        dialog.add_button("Later", Gtk.ResponseType.CANCEL)
        dialog.add_button("Open page", Gtk.ResponseType.ACCEPT)
        dialog.add_button("Download", Gtk.ResponseType.APPLY)
        body = w.vbox(spacing=T.S3)
        body.set_margin_top(T.S4)
        body.set_margin_bottom(T.S4)
        body.set_margin_start(T.S5)
        body.set_margin_end(T.S5)
        body.append(w.label(f"Telegram Downloader {info.latest_version} is "
                            f"available (you have {app_version()}).",
                            role="body", wrap=True))
        dialog.get_content_area().append(body)
        response = w.run_dialog(dialog)
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            try:
                updates.open_release_page(info.release_url)
            except Exception as exc:
                self.app.toast(f"Cannot open browser: {exc}", error=True)
        elif response == Gtk.ResponseType.APPLY:
            self._update_progress.update(done=0, total=0, finished=False)
            self._progress_open()
            self.engine.submit(self._download_installer(info))

    def _progress_open(self) -> None:
        """Build the progress dialog. Runs on the GTK thread.

        The bar is the point: the download takes long enough that a toast
        and silence reads as a broken app, which is exactly what the
        updater looked like before the bar existed.
        """
        dialog = Gtk.Dialog(title="Downloading the update",
                            transient_for=self.app.window, modal=True)
        body = w.vbox(spacing=T.S3)
        body.set_margin_top(T.S4)
        body.set_margin_bottom(T.S4)
        body.set_margin_start(T.S5)
        body.set_margin_end(T.S5)
        self._progress_bar = w.progress(None)
        self._progress_detail = w.label("Starting…", role="meta")
        body.append(self._progress_bar)
        body.append(self._progress_detail)
        dialog.get_content_area().append(body)
        dialog.show()
        self._progress_dialog = dialog
        self._progress_timer = GLib.timeout_add(200, self._progress_poll)

    def _progress_poll(self) -> bool:
        """Paint the bar. Returns False to stop the timer.

        The state is a plain dict the download thread writes and this
        reads, so neither side ever touches the other's widget.
        """
        state = self._update_progress
        done = int(state.get("done") or 0)
        total = int(state.get("total") or 0)
        if self._progress_bar is not None:
            if total:
                self._progress_bar.set_fraction(min(1.0, done / total))
                self._progress_detail.set_text(
                    f"{format_bytes(done)} of {format_bytes(total)}")
            else:
                # The size is not known yet: the bar pulses rather than
                # sitting at zero, which reads as "stuck".
                self._progress_bar.pulse()
                self._progress_detail.set_text(f"{format_bytes(done)} downloaded")
        if state.get("finished"):
            self._progress_close()
            return False
        return True

    def _progress_close(self) -> None:
        """Take the dialog down. Runs on the GTK thread."""
        if self._progress_timer is not None:
            GLib.source_remove(self._progress_timer)
            self._progress_timer = None
        if self._progress_dialog is not None:
            self._progress_dialog.destroy()
            self._progress_dialog = None
        self._progress_bar = None
        self._progress_detail = None

    async def _download_installer(self, info) -> None:
        """Download the installer, with the bar moving.

        The verification is the shared updater's -- it is not reimplemented
        here. The download runs on a worker thread and the bar is painted
        by a poller on the GTK thread.
        """
        from ...config import ensure_download_dir
        asset = updates.installable_asset(info)
        if asset is None:
            self.app.toast("No installer for your OS -- opening release "
                           "page instead.", error=True)
            try:
                updates.open_release_page(info.release_url)
            except Exception:
                pass
            return

        def _on_progress(done: int, total) -> None:
            self._update_progress["done"] = int(done or 0)
            if total:
                self._update_progress["total"] = int(total)

        try:
            token = os.environ.get("GITHUB_TOKEN") or ""
            dest = await asyncio.to_thread(
                updates.download_asset, asset,
                ensure_download_dir(load_settings()), info.latest_version,
                token or None, progress=_on_progress)
        except Exception as exc:          # noqa: BLE001 - shown to the user
            self._update_progress["finished"] = True
            self.app.toast(f"Download failed: {exc}", error=True)
            return
        self._update_progress["finished"] = True
        # The confirmation is a GTK dialog: it has to be built on the GTK
        # thread, and this coroutine is on the engine's loop.
        self.engine.call_soon(self._confirm_install, dest)

    def _confirm_install(self, dest) -> None:
        """Ask, then install. Runs on the GTK thread."""
        go = self.app.confirm(
            "Install this update?",
            f"Downloaded and verified:\n{os.path.basename(str(dest))}\n\n"
            "Installing runs apt-get with administrator rights. Your normal "
            "password prompt will appear. The app should be closed while it "
            "updates.",
            ok_label="Install and restart")
        if not go:
            self.app.toast(f"Installer saved to {dest}.")
            return
        self.app.toast("Installing… approve the password prompt to continue.")
        self.engine.submit(self._install_and_restart(dest))

    async def _install_and_restart(self, dest) -> None:
        ok, msg = await asyncio.to_thread(updates.install_deb, dest)
        if ok:
            self.app.toast(msg)
            try:
                self.app.window.close()
            except Exception:
                pass
        else:
            self.app.toast(f"Install failed: {msg}", error=True)
            self.app.toast(f"The installer is still at {dest}.")

    # -- the danger zone --------------------------------------------------

    async def _on_reset(self) -> None:
        """Destructive and irreversible: clears the saved Telegram session,
        the credentials and the whole queue. A plain yes/no sat one stray
        click away from wiping a logged-in session, so this requires typing
        the word."""
        from ...config import clear_session
        from ...config import Settings

        phrase = await self.app.ask(
            "Reset everything?",
            "This signs you out of Telegram, deletes your saved API "
            "credentials, and empties the download queue. Files already "
            "downloaded are not touched. This cannot be undone.\n\n"
            f"Type {self._phrase} to confirm.",
            f"Type {self._phrase} to confirm", ok_label="Reset everything")
        if not phrase or phrase.strip() != self._phrase:
            return
        self.engine.cancel_batch()
        try:
            clear_session()
        except Exception:
            pass
        self.engine.clear_all()
        save_settings(Settings())
        try:
            # Drive the VISIBLE toggle, not a hidden control, so a reset
            # cannot leave the old mode on screen.
            self.app.views["add"].refresh()
        except Exception:
            pass
        self.app.toast("Reset complete. Re-enter API credentials.")

    _phrase = "RESET"
