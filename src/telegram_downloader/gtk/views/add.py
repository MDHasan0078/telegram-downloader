"""The Add destination: links in, the output mode, and the button that starts.

Ported from gui.py's downloads_view. The order is the app's current one:
the link field, the Output mode control with its blurb, then the actions.
The mode sits with the links it applies to and ABOVE the button that starts
the download, so the choice is made in the same breath as the action.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

from ...config import load_settings, save_settings  # noqa: E402
from ...constants import MAX_BATCH_BYTES  # noqa: E402
from ...telegram_utils import split_urls  # noqa: E402
from .. import widgets as w
from .. import tokens as T

# What each mode does, in words. The old Settings card showed only
# "Default: Original", which told the user nothing.
MODE_BLURB = {
    "1": "Keep the file exactly as Telegram sent it. No conversion, so "
         "nothing is ever re-encoded.",
    "2": "Convert to MP4 with a fast encoder preset. Smaller file, quicker to "
         "process.",
    "3": "Convert to MP4 with a better compression preset. Smallest file, "
         "slowest to process.",
}

PLACEHOLDER = "https://t.me/channel/123\n(one per line)"


class AddView:
    """The Add page."""

    def __init__(self, app):
        self.app = app
        self.engine = app.engine
        self._build()

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        page = w.vbox(spacing=T.S4)
        page.add_css_class("page")

        # -- the link field
        self.text = Gtk.TextView()
        self.text.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text.get_buffer().connect("changed", self._on_text_changed)
        self.text.set_size_request(-1, 5 * 20 + T.S3)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.text)
        # NOT vexpand: the field is five lines tall, and the page below it
        # starts immediately -- the tab strip is the page title, so there is
        # no heading and no filler to push the content down. The height goes
        # on the SCROLLER: a TextView's size request is only a preference,
        # and a ScrolledWindow shrinks to its child's minimum.
        scroller.set_size_request(-1, 5 * 20 + T.S3)

        # The hint lives INSIDE the field, over the text view, and steps
        # aside when there is text. A click on it still reaches the view.
        self._placeholder = w.label(PLACEHOLDER, role="meta")
        self._placeholder.set_valign(Gtk.Align.START)
        self._placeholder.set_margin_top(T.S2 + 4)
        self._placeholder.set_margin_start(T.S3 + 2)
        self._placeholder.set_sensitive(False)
        overlay = Gtk.Overlay()
        overlay.set_child(scroller)
        overlay.add_overlay(self._placeholder)
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._on_placeholder_click)
        self._placeholder.add_controller(click)

        self.counter = w.label("", role="meta")
        head = w.hbox(spacing=T.S2)
        title = w.label("Telegram link(s)", role="label")
        title.set_hexpand(True)
        head.append(title)
        head.append(self.counter)
        self._field_box = w.vbox(spacing=T.S1 + 2)
        self._field_box.append(head)
        self._field_box.append(overlay)
        page.append(self._field_box)

        # -- the output mode
        page.append(w.heading("Output mode"))
        st = load_settings()
        current = st.output_mode if st.output_mode in MODE_BLURB else "1"
        self.blurb = w.label(MODE_BLURB[current], role="meta", wrap=True)
        self.segmented = w.Segmented([("1", "Original"), ("2", "Fast"),
                                      ("3", "Max")], current, self._on_mode)
        # The control is the size of its segments, not the width of the page:
        # a trough stretched across the window reads as a toolbar, not as a
        # three-way choice.
        self.segmented.set_halign(Gtk.Align.START)
        mode_box = w.vbox(spacing=T.S2)
        mode_box.append(self.segmented)
        mode_box.append(self.blurb)
        page.append(mode_box)

        # -- the actions
        actions = w.hbox(spacing=T.S2)
        actions.append(w.button("Add", kind="primary", icon="list-add",
                                on_click=self._on_add))
        actions.append(w.button("Load .txt", kind="secondary",
                                icon="document-open", on_click=self._on_load))
        actions.append(w.button("Clear", kind="secondary", icon="edit-clear",
                                on_click=self._on_clear))
        page.append(actions)

        # -- the status line (hidden when there is nothing to say)
        self.status_dot = w.state_dot("waiting", size=7)
        self.status_txt = w.label("", role="meta")
        self.status_row = w.hbox(spacing=T.S2)
        self.status_row.append(self.status_dot)
        self.status_row.append(self.status_txt)
        self.status_row.set_visible(False)
        page.append(self.status_row)

        self.widget = Gtk.ScrolledWindow()
        self.widget.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.widget.set_child(page)
        self.widget.set_vexpand(True)
        self._update_placeholder()

    # -- helpers ----------------------------------------------------------

    def _set_status(self, text: str) -> None:
        """One writer, so no call site has to remember to clear both the
        text and the row that holds it."""
        self.status_txt.set_text(text or "")
        self.status_row.set_visible(bool(text))

    def _update_placeholder(self, *_args) -> None:
        """The hint inside the field, shown while it is empty."""
        buf = self.text.get_buffer()
        self._placeholder.set_visible(buf.get_char_count() == 0)

    def _on_placeholder_click(self, _gesture, _n_press, _x, _y) -> None:
        """A click on the hint still puts the caret in the field."""
        self.text.grab_focus()

    def _on_text_changed(self, *_args) -> None:
        urls = split_urls(self.text.get_buffer().get_text(
            self.text.get_buffer().get_start_iter(),
            self.text.get_buffer().get_end_iter(), False) or "")
        n = len(urls)
        self.counter.set_text(f"{n} URL{'s' if n != 1 else ''} detected"
                              if n else "")

    def refresh(self) -> None:
        """The mode can be changed elsewhere (a reset), so repaint it."""
        st = load_settings()
        current = st.output_mode if st.output_mode in MODE_BLURB else "1"
        self.segmented.set_value(current)
        self.blurb.set_text(MODE_BLURB[current])

    # -- the actions ------------------------------------------------------

    def _on_mode(self, value: str) -> None:
        """The SINGLE writer for the output mode."""
        st = load_settings()
        st.output_mode = value
        save_settings(st)
        self.blurb.set_text(MODE_BLURB[value])
        self.segmented.set_value(value)

    def _on_add(self, _btn) -> None:
        if self.engine.queuing:
            return      # double-click / repeat click must not double-queue
        self._add_async()

    def _add_async(self) -> None:
        """The Add action, on the engine's loop.

        Ported from gui.py's on_add: the credentials are checked first (and
        the user is sent to Account auth if they are missing), then the URLs
        are split, queued, and the run starts.
        """
        async def _do():
            st = load_settings()
            if not st.api_id or not st.api_hash:
                self.app.toast("First add your Telegram API ID + hash in "
                               "Account auth.", error=True)
                self.app.goto("auth")
                return
            buf = self.text.get_buffer()
            urls = split_urls(buf.get_text(buf.get_start_iter(),
                                           buf.get_end_iter(), False) or "")
            if not urls:
                self.app.toast("Paste at least one t.me link first.",
                               error=True)
                return
            self.engine.queuing = True
            self._set_status(f"Adding {len(urls)} link(s)…")
            try:
                added = self.engine.add_items(urls, st.output_mode or "1")
                if added < len(urls):
                    self.app.toast(f"Queue near full: added {added} of "
                                   f"{len(urls)} link(s).", error=True)
                else:
                    self.app.toast(f"Downloading {added} link(s).")
                buf.set_text("")
            finally:
                self.engine.queuing = False
                self._set_status("")
            self.app.goto("ongoing")
            self.engine.start_queue_async()

        self.engine.submit(_do())

    def _on_clear(self, _btn) -> None:
        buf = self.text.get_buffer()
        buf.set_text("")
        self._set_status("")

    def _on_load(self, _btn) -> None:
        """Load a .txt batch file.

        GTK returns BYTES, so there is no path-with-O_NOFOLLOW dance here --
        but the size cap is still checked, because a 4 GB .txt is not a
        batch file.
        """
        def _opened(source, result) -> None:
            try:
                file = source.open_finish(result)
            except Exception:
                return
            try:
                info = file.query_info("standard::size",
                                       Gio.FileQueryInfoFlags.NONE)
                size = info.get_size()
            except Exception:
                size = 0
            if size > MAX_BATCH_BYTES:
                self.app.toast("Batch file too large (max 256 KB). "
                               "Split it or trim it.", error=True)
                return
            try:
                data = file.read_bytes()
            except Exception as exc:
                self.app.toast(f"Cannot read file: {exc}", error=True)
                return
            text = bytes(data).decode("utf-8", errors="replace")
            urls = split_urls(text)
            buf = self.text.get_buffer()
            buf.set_text("\n".join(urls))
            self.app.toast(f"Loaded {len(urls)} URL(s) from file.")

        dialog = Gtk.FileDialog(title="Choose URL list")
        filter = Gtk.FileFilter()
        filter.set_name("Text files")
        filter.add_pattern("*.txt")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter)
        dialog.set_filters(filters)
        dialog.open(self.app.window, None, _opened)
