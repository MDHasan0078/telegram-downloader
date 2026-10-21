"""The Account auth destination: the API credentials and the Telegram login.

Ported from gui.py's auth_view. This is where the credentials live -- it is
its own destination, not a row buried in Settings -- so the copy that sends
the user here ("First add your Telegram API ID + hash in Account auth") and
the copy here agree.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ...config import (clear_session, harden_session_files, load_settings,
                      save_settings)
from .. import tokens as T
from .. import widgets as w


class AuthView:
    """The Account auth page."""

    def __init__(self, app):
        self.app = app
        self.engine = app.engine
        self._build()

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        page = w.vbox(spacing=T.S4)
        page.add_css_class("page")

        st = load_settings()
        first = not st.api_id or not st.api_hash

        self.badge = w.badge("UNSET" if first else "STORED")
        page.append(w.heading("Telegram account", trailing=self.badge))
        if first:
            page.append(w.notice(
                "Create these at my.telegram.org → API development tools. "
                "They are stored privately (0600) and only asked once."))

        # The two fields side by side.
        self.api_id = Gtk.Entry()
        self.api_id.set_text(st.api_id or "")
        self.api_hash = Gtk.Entry()
        self.api_hash.set_text(st.api_hash or "")
        self.api_hash.set_visibility(False)
        self.api_hash.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        reveal = Gtk.Button()
        reveal.add_css_class("icon")
        reveal.set_child(Gtk.Image.new_from_icon_name("view-reveal-symbolic"))
        reveal.set_tooltip_text("Show the value")
        reveal.connect("clicked", self._on_reveal)
        hash_row = w.hbox(spacing=T.S1)
        hash_row.append(self.api_hash)
        hash_row.append(reveal)

        grid = Gtk.Grid(column_spacing=T.S3, row_spacing=T.S3)
        grid.attach(w.field(self.api_id, label_text="API ID",
                            hint="my.telegram.org → App api_id"), 0, 0, 1, 1)
        grid.attach(w.field(hash_row, label_text="API hash",
                            hint="128-bit hex string"), 1, 0, 1, 1)
        # Two halves, like the ResponsiveRow the Flet build used: the fields
        # are peers, and a narrow hash field next to a wide id field reads
        # as two different kinds of thing.
        grid.set_column_homogeneous(True)
        self.api_id.set_hexpand(True)
        self.api_hash.set_hexpand(True)
        page.append(grid)

        self.error = w.notice("API ID must be numeric.", kind="error")
        self.error.set_visible(False)
        page.append(self.error)

        actions = w.hbox(spacing=T.S2)
        self._save_button = w.pending_button(
            "Save", "document-save", self._on_save, kind="primary",
            runner=self.app.run_async)
        actions.append(self._save_button)
        actions.append(w.pending_button(
            "Verify / Login", "system-users", self._on_login,
            kind="secondary", runner=self.app.run_async))
        actions.append(w.pending_button(
            "Log out", "system-log-out", self._on_logout,
            kind="secondary", runner=self.app.run_async))
        page.append(actions)

        self.widget = page
        self.widget.set_vexpand(True)

    # -- helpers ----------------------------------------------------------

    def _on_reveal(self, _btn) -> None:
        shown = self.api_hash.get_visibility()
        self.api_hash.set_visibility(not shown)

    def refresh(self) -> None:
        """The stored credentials may have changed (a reset, a CLI edit)."""
        st = load_settings()
        first = not st.api_id or not st.api_hash
        self.badge.set_text("UNSET" if first else "STORED")

    # -- the actions ------------------------------------------------------

    async def _on_save(self) -> None:
        s = load_settings()
        s.api_id = self.api_id.get_text().strip() or None
        s.api_hash = self.api_hash.get_text().strip() or None
        if s.api_id and not s.api_id.isdigit():
            self.error.set_visible(True)
            return
        save_settings(s)
        self.error.set_visible(False)
        self.refresh()
        self.app.toast("Credentials saved.")

    async def _on_login(self) -> None:
        if not self.api_id.get_text().strip() or \
                not self.api_hash.get_text().strip():
            self.app.toast("Save API ID + hash first.", error=True)
            return
        s = load_settings()
        s.api_id = self.api_id.get_text().strip()
        s.api_hash = self.api_hash.get_text().strip()
        save_settings(s)
        try:
            c = await self.engine.get_client()
            if await c.is_user_authorized():
                me = await c.get_me()
                name = (getattr(me, "first_name", None)
                        or getattr(me, "username", None))
                self.app.toast(f"Already logged in as {name}.")
                return
            ok = await self._login_dialog(c)
            self.app.toast("Login OK." if ok else "Login cancelled.")
        except Exception as exc:          # noqa: BLE001 - shown to the user
            self.app.toast(f"Login failed: {exc}", error=True)

    async def _login_dialog(self, client) -> bool:
        """The phone / code / 2FA dialogs.

        The dialogs are modal and blocking, so they are asked for on the GTK
        thread and awaited here on the engine's loop.
        """
        from .. import tg_client
        try:
            from telethon import errors as terr
        except Exception:                 # pragma: no cover - telethon missing
            terr = None

        phone = await self.app.ask("Telegram login", "",
                                   "Phone number or bot token")
        if not phone:
            return False
        is_phone = tg_client.looks_like_phone(phone)

        async def _code_callback():
            return await self.app.ask("Enter the code we just sent", "",
                                      "Login code from Telegram") or ""

        try:
            if is_phone:
                await client.start(phone=phone, code_callback=_code_callback)
            else:
                await client.start(bot_token=phone)
            try:
                me = await client.get_me()
                try:
                    # Pin the account this session belongs to. get_client()
                    # refuses to reuse a session for a different user.
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
        except Exception as exc:          # noqa: BLE001 - reported below
            if terr is not None and isinstance(exc, terr.FloodWaitError):
                self.app.toast(f"Telegram rate-limit: wait {exc.seconds}s.",
                               error=True)
                return False
            if terr is not None and isinstance(exc,
                                               terr.SessionPasswordNeededError):
                # Two-step verification: ask for the password and sign in.
                pw = await self.app.ask("2-step verification", "",
                                        "2FA password", password=True)
                if not pw:
                    return False
                try:
                    await client.sign_in(password=pw)
                except Exception:
                    self.app.toast("Login failed.", error=True)
                    return False
                harden_session_files()
                return True
            if terr is not None and isinstance(exc, terr.PhoneCodeInvalidError):
                self.app.toast("Wrong code. Request a new one and try again.",
                               error=True)
                return False
        if not is_phone:
            # Bot tokens never take a login code: don't prompt for one
            # (avoids an unbounded code-guessing loop on tokens).
            self.app.toast("Bot login failed. Check the token and try again.",
                           error=True)
            return False
        return False

    async def _on_logout(self) -> None:
        """Sign out of Telegram.

        Matches `tg-dl config logout` exactly, because the two are the same
        operation and must not drift: the API id/hash belong to the APP and
        are kept, the pinned identity is cleared, and the local session file
        is removed.
        """
        server_ok = False
        try:
            c = await self.engine.get_client()
            await c.log_out()
            server_ok = True
        except Exception:
            pass
        # The pin is what stops a planted session from being adopted, so
        # signing out has to clear it -- otherwise the one escape hatch the
        # pin documents ("Log out, then sign in again") stays blocked.
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
        self.refresh()
        # A local-only logout leaves the session alive on Telegram's side.
        # Say so plainly instead of implying a clean sign-out.
        leftover = False
        try:
            from ...config import CONFIG_DIR as _CD
            leftover = any(_CD.glob("session*"))
        except OSError:
            leftover = False
        if leftover:
            self.app.toast("Warning: session files remain (locked?). "
                           + ("Server logout ok." if server_ok else
                              "Server logout FAILED — revoke at my.telegram.org."),
                           error=True)
        elif not server_ok:
            self.app.toast("Local session removed, but server logout FAILED "
                           "(offline?). Revoke at my.telegram.org if shared.",
                           error=True)
        else:
            self.app.toast("Logged out — server session revoked, "
                           "files removed.")
