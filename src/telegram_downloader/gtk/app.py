"""The GTK4 window: the top bar, the page stack, the download-folder bar.

The shell owns the chrome. The four destinations live in `views/`; the
shared behaviour lives in `engine.py`; the colours live in `theme.py`.

The top bar is ONE row and it is the navigation:

    [logo] Telegram Downloader │ Add  Ongoing  ······  [☀]  Account auth  Settings

The theme toggle sits in the gap rather than at the end because it is
chrome, not a destination, and it gets air on its right so it never reads
as a fifth tab. There is no per-page heading: the chip strip IS the page
title, and the selected chip is a RAISED surface with a hairline -- never an
accent fill, because colour in this app means action or status, not "where
am I".

The bottom bar is pinned on every destination: the download folder, the
folder's own "open" button and a Change button. It is the ONE place the
folder is chosen.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from ..config import load_settings, save_settings  # noqa: E402
from . import theme as theme_mod
from . import tokens as T
from . import widgets as w
from .engine import Engine
from .views import add as add_view
from .views import auth as auth_view
from .views import ongoing as ongoing_view
from .views import settings as settings_view

APP_ID = "org.mdhasan0078.telegram-downloader"

# The logo: the packaged asset, loaded from its path. gui.py's app_logo_src()
# returns a data URI, but importing gui.py would drag in the whole Flet
# client (and telethon) for one SVG -- and GTK wants a path, not a data URI.
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "icon.svg"

# The four destinations, in the order the bar shows them.
DESTINATIONS = [
    ("add", "Add"),
    ("ongoing", "Ongoing"),
    ("auth", "Account auth"),
    ("settings", "Settings"),
]


class App(Gtk.Application):
    """The application: one window, one top bar, four destinations."""

    def __init__(self, engine: Engine | None = None):
        super().__init__(application_id=APP_ID)
        self.engine = engine if engine is not None else Engine()
        self.window: Gtk.ApplicationWindow | None = None
        self._stack: Gtk.Stack | None = None
        self._chips: list[Gtk.Button] = []
        self._theme_button: Gtk.Button | None = None
        self._dl_label: Gtk.Label | None = None
        self._toast_revealer: Gtk.Revealer | None = None
        self._toast_label: Gtk.Label | None = None
        self._toast_timer = None
        self._theme = "dark"
        self._dl_path = ""
        self.views = {}

    # -- lifecycle --------------------------------------------------------

    def do_activate(self) -> None:  # noqa: N802 (GTK naming)
        self.engine.start()
        self.engine.on_queue_changed = self._on_queue_changed
        self.engine.on_message = self._on_message

        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("Telegram Downloader")
        self.window.set_default_size(1180, 780)
        self.window.set_size_request(760, 480)

        st = load_settings()
        self._theme = st.theme or "dark"
        theme_mod.apply(self._theme == "dark")

        self._build()
        self.window.present()

    def _build(self) -> None:
        win = self.window
        assert win is not None

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self._build_top_bar())

        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self.views["add"] = add_view.AddView(self)
        self.views["ongoing"] = ongoing_view.OngoingView(self)
        self.views["auth"] = auth_view.AuthView(self)
        self.views["settings"] = settings_view.SettingsView(self)
        for key, view in self.views.items():
            self._stack.add_named(view.widget, key)
        root.append(self._stack)

        root.append(self._build_bottom_bar())

        # The toast: a Revealer over the window, pinned to the bottom.
        overlay = Gtk.Overlay()
        overlay.set_child(root)
        self._toast_revealer = Gtk.Revealer()
        self._toast_revealer.set_reveal_child(False)
        self._toast_revealer.set_transition_type(
            Gtk.RevealerTransitionType.CROSSFADE)
        self._toast_revealer.set_transition_duration(180)
        toast_box = w.hbox(spacing=T.S2)
        toast_box.add_css_class("toast")
        self._toast_label = w.label("", role="body", wrap=True)
        toast_box.append(self._toast_label)
        self._toast_revealer.set_child(toast_box)
        toast_box.set_halign(Gtk.Align.CENTER)
        toast_box.set_valign(Gtk.Align.END)
        toast_box.set_margin_bottom(T.S6)
        overlay.add_overlay(self._toast_revealer)
        win.set_child(overlay)

        # The app opens where the user left off: without credentials there is
        # nothing to add, so the Account auth tab is the only useful screen.
        st = load_settings()
        start = "auth" if not st.has_api else "add"
        self.goto(start, force=True)

    # -- the top bar ------------------------------------------------------

    def _build_top_bar(self) -> Gtk.Box:
        bar = w.hbox(spacing=T.S2)
        bar.add_css_class("topbar")

        # brand
        logo = self._logo_image()
        if logo is not None:
            bar.append(logo)
        brand = w.label("Telegram Downloader", role="strong")
        brand.add_css_class("brand")
        bar.append(brand)
        bar.append(w.vdivider())

        # The two primary destinations...
        for key, title in DESTINATIONS[:2]:
            bar.append(self._nav_chip(key, title))
        # ...the flexible gap...
        bar.append(w.spacer())
        # ...the theme toggle, which is chrome and not a destination...
        self._theme_button = Gtk.Button()
        self._theme_button.add_css_class("theme")
        self._theme_button.set_child(Gtk.Image.new_from_icon_name("weather-clear-symbolic"))
        # Air on the right: the theme toggle is chrome, not a destination,
        # and it must not read as a fifth tab.
        self._theme_button.set_margin_end(T.S2)
        self._theme_button.connect("clicked", self._on_theme_clicked)
        bar.append(self._theme_button)
        # ...and the two settings-side destinations.
        for key, title in DESTINATIONS[2:]:
            bar.append(self._nav_chip(key, title))

        self._update_theme_icon()
        return bar

    def _nav_chip(self, key: str, title: str) -> Gtk.Button:
        chip = Gtk.Button()
        chip.add_css_class("nav")
        chip.set_label(title)
        chip.set_tooltip_text(title)
        chip.connect("clicked", lambda _b, k=key: self.goto(k))
        self._chips.append((key, chip))
        return chip

    def _logo_image(self) -> Gtk.Widget | None:
        """The packaged 'Descent' mark, loaded through GdkPixbuf.

        GTK4's own image loader has no SVG backend; GdkPixbuf does, and the
        asset ships with the package, so there is nothing to download.
        """
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file(str(LOGO_PATH))
            pix = pix.scale_simple(24, 24, GdkPixbuf.InterpType.BILINEAR)
            image = Gtk.Image.new_from_paintable(
                Gdk.Texture.new_for_pixbuf(pix))
            return image
        except Exception:
            return None

    # -- the bottom bar ---------------------------------------------------

    def _build_bottom_bar(self) -> Gtk.Box:
        bar = w.hbox(spacing=T.S2)
        bar.add_css_class("bottombar")
        bar.append(Gtk.Image.new_from_icon_name("folder-symbolic"))
        self._dl_label = w.label(self.engine.folder_label(), role="meta",
                                 ellipsize=True)
        self._dl_label.set_hexpand(True)
        bar.append(self._dl_label)
        bar.append(w.icon_button("folder-open", "Open download folder",
                                  self._on_open_folder))
        bar.append(w.button("Change", kind="secondary",
                             on_click=self._on_change_folder))
        return bar

    def refresh_folder_label(self) -> None:
        if self._dl_label is not None:
            self._dl_label.set_text(self.engine.folder_label())

    def _on_open_folder(self, _btn) -> None:
        self.engine.open_folder()

    def _on_change_folder(self, _btn) -> None:
        """Ask for a folder, validate it, store it.

        The chooser is a Gtk.FileDialog with select_folder=True; the
        validation and the save are the shared engine's, so the bar can
        never display a folder that download time would refuse.
        """
        def _opened(source, result) -> None:
            try:
                folder = source.select_folder_finish(result)
            except Exception:
                return
            if not folder:
                return
            path = folder.get_path()
            if not path:
                return
            self._folder_chosen(path)

        dialog = Gtk.FileDialog(title="Choose download folder",
                                select_folder=True)
        dialog.select_folder(self.window, None, _opened)

    def _folder_chosen(self, path: str) -> None:
        """A folder was picked: validate it, store it, repaint the bar."""
        if self.engine.set_download_dir(path):
            self.refresh_folder_label()

    # -- navigation -------------------------------------------------------

    def goto(self, key: str, force: bool = False) -> None:
        """Switch destination. The selected chip is a raised pill."""
        if self._stack is None:
            return
        if not force and self._stack.get_visible_child_name() == key:
            return
        self._stack.set_visible_child_name(key)
        for chip_key, chip in self._chips:
            if chip_key == key:
                chip.add_css_class("selected")
            else:
                chip.remove_css_class("selected")
        if key == "ongoing":
            self.views["ongoing"].refresh()
        elif key == "auth":
            self.views["auth"].refresh()
        elif key == "settings":
            self.views["settings"].refresh()
        elif key == "add":
            self.views["add"].refresh()

    # -- the theme ---------------------------------------------------------

    def _on_theme_clicked(self, _btn) -> None:
        new_theme = "light" if self._theme == "dark" else "dark"
        self._theme = new_theme
        theme_mod.apply(new_theme == "dark")
        try:
            st = load_settings()
            st.theme = new_theme
            save_settings(st)
        except Exception:
            pass
        self._update_theme_icon()

    def _update_theme_icon(self) -> None:
        """The icon shows the theme the button switches TO."""
        if self._theme_button is None:
            return
        name = ("weather-clear-symbolic" if self._theme == "dark"
                else "weather-clear-night-symbolic")
        self._theme_button.set_child(
            Gtk.Image.new_from_icon_name(name))
        self._theme_button.set_tooltip_text(
            "Switch to light mode" if self._theme == "dark"
            else "Switch to dark mode")

    # -- the toast ---------------------------------------------------------

    def _on_message(self, message: str, error: bool = False) -> None:
        """Called from the engine thread; hop to the GTK thread."""
        GLib.idle_add(self.toast, message, error)

    def toast(self, message: str, error: bool = False) -> bool:
        """A transient message at the bottom of the window."""
        if self._toast_label is None or self._toast_revealer is None:
            return False
        self._toast_label.set_text(message)
        box = self._toast_revealer.get_child()
        if box is not None:
            if error:
                box.add_css_class("error")
            else:
                box.remove_css_class("error")
        self._toast_revealer.set_reveal_child(True)
        if self._toast_timer is not None:
            GLib.source_remove(self._toast_timer)
        self._toast_timer = GLib.timeout_add(4500, self._hide_toast)
        return False

    def _hide_toast(self) -> bool:
        if self._toast_revealer is not None:
            self._toast_revealer.set_reveal_child(False)
        self._toast_timer = None
        return False

    # -- the queue ---------------------------------------------------------

    def _on_queue_changed(self) -> None:
        """Called from the engine thread; hop to the GTK thread."""
        GLib.idle_add(self._refresh_queue)

    def _refresh_queue(self) -> bool:
        view = self.views.get("ongoing")
        if view is not None:
            view.refresh()
        return False

    # -- helpers for the views --------------------------------------------

    def run_async(self, handler, done) -> None:
        """Run a coroutine function off the GTK thread and report back on
        it. Used by every long action (save, login, update check)."""
        self.engine.run_async(handler, done)

    def prompt(self, title: str, body: str, field_label: str, *,
               password: bool = False, ok_label: str = "OK",
               icon: str | None = None) -> str | None:
        """A modal prompt. Returns the text, or None if cancelled.

        Blocking, but it runs on the GTK thread and the caller is a GTK
        callback, so nothing else is waiting on the loop.
        """
        dialog = Gtk.Dialog(title=title, transient_for=self.window,
                            modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(ok_label, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        body_box = w.vbox(spacing=T.S3)
        body_box.set_margin_top(T.S4)
        body_box.set_margin_bottom(T.S4)
        body_box.set_margin_start(T.S5)
        body_box.set_margin_end(T.S5)
        if body:
            body_box.append(w.label(body, role="body", wrap=True))
        entry = Gtk.Entry()
        if password:
            entry.set_visibility(False)
            entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        body_box.append(w.field(entry, label_text=field_label))
        dialog.get_content_area().append(body_box)
        dialog.show_all()
        response = dialog.run()
        text = entry.get_text().strip() if response == Gtk.ResponseType.OK else None
        entry.set_text("")
        dialog.destroy()
        return text or None

    def confirm(self, title: str, body: str, *, ok_label: str = "OK",
                danger: bool = False) -> bool:
        """A yes/no confirmation."""
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(ok_label, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        body_box = w.vbox(spacing=T.S3)
        body_box.set_margin_top(T.S4)
        body_box.set_margin_bottom(T.S4)
        body_box.set_margin_start(T.S5)
        body_box.set_margin_end(T.S5)
        body_box.append(w.label(body, role="body", wrap=True))
        dialog.get_content_area().append(body_box)
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def ask(self, title: str, body: str, field_label: str, *,
            password: bool = False, ok_label: str = "OK") -> "asyncio.Future":
        """Ask a question from the ENGINE thread; returns a future.

        A GTK dialog must be built on the GTK thread, but the code that
        needs the answer runs on the engine's loop -- so the dialog is
        opened here and the answer is carried back in a future.
        """
        fut = asyncio.get_running_loop().create_future()

        def _ask() -> bool:
            def _got(text):
                if not fut.done():
                    fut.set_result(text)
            self.prompt_async(title, body, field_label, callback=_got,
                              password=password, ok_label=ok_label)
            return False
        GLib.idle_add(_ask)
        return fut

    def prompt_async(self, title: str, body: str, field_label: str, *,
                     callback, password: bool = False,
                     ok_label: str = "OK", value: str = "") -> None:
        """A non-blocking modal prompt. The callback gets the text, or None
        if the user cancelled."""
        dialog = Gtk.Dialog(title=title, transient_for=self.window,
                            modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(ok_label, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        body_box = w.vbox(spacing=T.S3)
        body_box.set_margin_top(T.S4)
        body_box.set_margin_bottom(T.S4)
        body_box.set_margin_start(T.S5)
        body_box.set_margin_end(T.S5)
        if body:
            body_box.append(w.label(body, role="body", wrap=True))
        entry = Gtk.Entry()
        entry.set_text(value or "")
        if password:
            entry.set_visibility(False)
            entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        body_box.append(w.field(entry, label_text=field_label))
        dialog.get_content_area().append(body_box)

        def _done(_d, response):
            text = None
            if response == Gtk.ResponseType.OK:
                text = entry.get_text().strip() or None
            entry.set_text("")           # best-effort secret wipe
            dialog.destroy()
            callback(text)
        dialog.connect("response", _done)
        dialog.show_all()


def run_gtk() -> int:
    """Launch the GTK4 GUI. Returns the process exit code."""
    app = App()
    return app.run([])


if __name__ == "__main__":       # pragma: no cover - manual launch
    raise SystemExit(run_gtk())
