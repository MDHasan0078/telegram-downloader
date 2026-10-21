"""The Ongoing destination: the queue, live.

Ported from gui.py's queue_view. A row is a LINE of the list, not a card:
the status dot leads it, the title sits above the detail, and a hairline
separates the rows. The repaint updates the live widgets in place -- the
same content is never rebuilt, because rebuilding would drop the scroll
position four times a second.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .. import tokens as T
from .. import widgets as w
from ..engine import _IDLE_LABEL, _STORE_TO_UI, progress_detail, row_sig

# The same seven store states -> the six-state mark vocabulary.
_RANK = {"downloading": 0, "converting": 1, "fetching": 2,
         "queued": 3, "done": 4, "cancelled": 5, "error": 6}


class OngoingView:
    """The Ongoing page."""

    def __init__(self, app):
        self.app = app
        self.engine = app.engine
        self._rows: dict = {}
        self._build()

    # -- construction -----------------------------------------------------

    def _build(self) -> None:
        page = w.vbox(spacing=T.S4)
        page.add_css_class("page")

        # -- the toolbar
        self.count = w.label("Downloads (0)", role="strong")
        self.summary = w.label("", role="meta", xalign=1.0)
        self.summary.set_hexpand(True)
        toolbar = w.hbox(spacing=T.S2)
        toolbar.append(self.count)
        toolbar.append(self.summary)
        toolbar.append(w.button("Clear finished", kind="quiet",
                                icon="edit-clear-all",
                                on_click=lambda _b: self.engine.clear_finished()))
        toolbar.append(w.button("Resume all / Start", kind="secondary",
                                icon="media-playback-start",
                                on_click=self._on_resume_all))
        toolbar.append(w.button("Cancel batch", kind="secondary",
                                icon="media-playback-stop",
                                on_click=lambda _b: self.engine.cancel_batch()))
        page.append(toolbar)

        # -- the empty state
        empty = w.vbox(spacing=T.S3)
        empty.set_valign(Gtk.Align.CENTER)
        empty.set_vexpand(True)
        # The mark sits in a disc, the way ui.empty() draws it: a bare glyph
        # on the page reads as a stray icon.
        disc = Gtk.Box()
        disc.set_size_request(52, 52)
        disc.set_halign(Gtk.Align.CENTER)
        disc.add_css_class("empty-mark")
        mark = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        mark.set_halign(Gtk.Align.CENTER)
        mark.set_valign(Gtk.Align.CENTER)
        disc.append(mark)
        empty.append(disc)
        empty.append(w.label("No downloads in queue", role="title",
                             xalign=0.5))
        empty.append(w.label("Add links from the Add tab to get started.",
                             role="meta", xalign=0.5))
        add_btn = w.button("Add downloads", kind="primary", icon="list-add",
                           on_click=lambda _b: self.app.goto("add"))
        empty.append(add_btn)
        self.empty = empty

        # -- the list
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.add_css_class("list")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list)
        scroller.set_vexpand(True)

        body = Gtk.Overlay()
        body.set_child(scroller)
        body.add_overlay(empty)
        page.append(body)

        self.widget = page
        self.widget.set_vexpand(True)
        self.refresh()

    # -- the actions ------------------------------------------------------

    def _on_resume_all(self, _btn) -> None:
        self.engine.reset_errors_to_queued()
        self.engine.start_queue_async()

    # -- the repaint ------------------------------------------------------

    def refresh(self) -> None:
        """Repaint the queue. Ported from gui.py's refresh_queue()."""
        items = list(self.engine.store.items)
        idx = {it.id: k for k, it in enumerate(self.engine.store.items)}
        # Active + queued first, finished at the bottom (newest first within
        # each group).
        items.sort(key=lambda i: (_RANK.get(i.status, 7), -idx.get(i.id, 0)))

        n_total = len(items)
        n_active = sum(1 for i in items
                       if i.status in ("downloading", "converting", "fetching"))
        n_queued = sum(1 for i in items if i.status == "queued")
        n_failed = sum(1 for i in items if i.status in ("error", "cancelled"))
        n_done = sum(1 for i in items if i.status == "done")

        self.count.set_text(f"Downloads ({n_total})")
        self.summary.set_markup(self._summary_markup(
            n_active, n_queued, n_done, n_failed, bool(items)))

        current = {it.id for it in items}
        for old_id in list(self._rows.keys()):
            if old_id not in current:
                # The row goes with the entry: dropping the entry alone
                # leaves its row in the list forever, and the list grows a
                # duplicate every repaint.
                stale = self._rows.pop(old_id)
                self.list.remove(stale["row"])

        self.empty.set_visible(not items)
        self.list.set_visible(bool(items))

        # Empty the list of whatever is IN it, not of what the dict
        # remembers: the two can disagree (a row whose item was pruned
        # above), and appending into a list that still holds the old rows
        # is how a queue ends up showing every item twice.
        child = self.list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt

        for it in items:
            sig = row_sig(it)
            entry = self._rows.get(it.id)
            if entry is not None and entry["sig"] == sig:
                # Same content: update the live widgets in place instead of
                # rebuilding the row.
                bar = entry["bar"]
                if bar is not None:
                    if it.status == "fetching":
                        bar.pulse()
                    else:
                        bar.set_fraction(max(0.0, min(1.0,
                                                       (it.progress or 0) / 100)))
                detail = entry["detail"]
                if detail is not None and it.status in (
                        "downloading", "converting", "fetching"):
                    # Only a LIVE row's line is the progress line. The
                    # Flet build overwrote every row's detail with it (the
                    # queued row's text is a different widget there, so the
                    # overwrite silently did nothing); here the detail IS a
                    # label, so a queued row would start claiming
                    # "downloading…" the moment anything else moved.
                    detail.set_text(progress_detail(it))
                self.list.append(entry["row"])
                continue
            self._build_row(it)

    def _summary_markup(self, n_active, n_queued, n_done, n_failed,
                        any_items) -> str:
        """The counters between the count and the buttons.

        Only counters that are non-zero are rendered: "0 need attention"
        put an alarm signal on a healthy queue. Plain text, and only "need
        attention" keeps its colour, because that one is asking for
        something.
        """
        if not any_items:
            return "Nothing here yet."
        muted = "#B4BBC3"
        error = "#F25451"
        parts = []

        def _count(n, label, colour):
            if n <= 0:
                return ""
            return f"<span foreground='{colour}'>{n} {label}</span>  "
        parts.append(_count(n_active, "active", muted))
        parts.append(_count(n_queued, "queued", muted))
        parts.append(_count(n_done, "done", muted))
        parts.append(_count(n_failed, "need attention", error))
        if not any(parts):
            return "All clear."
        return "".join(parts).rstrip()

    def _build_row(self, it) -> None:
        """One queue row."""
        ui_status = _STORE_TO_UI.get(it.status, "waiting")
        title = w.label(_sanitize(f"{it.title}{it.ext}", 120), role="title",
                        ellipsize=True)

        bar = None
        detail = None
        if it.status in ("downloading", "converting", "fetching"):
            bar = w.progress(None if it.status == "fetching"
                             else (it.progress or 0) / 100)
            detail = w.label(progress_detail(it), role="meta")
            meta = w.vbox(spacing=T.S1)
            meta.append(detail)
            meta.append(bar)
        elif it.status in ("queued", "cancelled"):
            # The row's leading dot already carries the state; the Flet
            # build drew a second one here, and two identical marks read
            # as a rendering fault rather than as a status.
            detail = w.label(_IDLE_LABEL.get(it.status, ""), role="meta")
            meta = detail
        elif it.status == "done" and it.dest:
            detail = w.label(_sanitize(it.dest), role="meta", mono=True,
                             ellipsize=True)
            meta = detail
        elif it.status == "error" and it.error:
            detail = w.label(_sanitize(it.error), role="meta",
                             color="error-text", wrap=True)
            meta = detail
        elif it.status == "done" and it.error:
            # Kept-original note (e.g. ffmpeg missing) -- neutral, not loud.
            detail = w.label(_sanitize(it.error), role="meta", wrap=True)
            meta = detail
        else:
            meta = None

        actions = w.hbox(spacing=T.S1)
        if it.status in ("error", "cancelled"):
            actions.append(w.icon_button(
                "view-refresh", "Retry",
                lambda _b, _id=it.id: self.engine.retry_item(_id)))
        if it.status in ("downloading", "converting", "fetching"):
            actions.append(w.icon_button(
                "media-playback-pause",
                "Pause this download (it resumes from where it stopped)",
                lambda _b, _id=it.id: self.engine.toggle_pause(_id)))
        else:
            actions.append(w.icon_button(
                "window-close", "Remove",
                lambda _b, _id=it.id: self.engine.remove_item(_id),
                kind="icon danger"))

        row = Gtk.ListBoxRow()
        row.add_css_class("row")
        line = w.hbox(spacing=T.S3)
        line.append(w.state_dot(ui_status, size=9))
        middle = w.vbox(spacing=T.S1)
        middle.set_hexpand(True)
        middle.append(title)
        if meta is not None:
            middle.append(meta)
        line.append(middle)
        line.append(actions)
        row.set_child(line)
        self._rows[it.id] = {"row": row, "bar": bar, "detail": detail,
                             "sig": row_sig(it)}
        self.list.append(row)


def _sanitize(value: str, limit: int = 500) -> str:
    from ...config import _sanitize_display
    return _sanitize_display(value, limit)
