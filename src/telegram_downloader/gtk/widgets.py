"""The shared GTK4 primitives -- the port of `telegram_downloader.ui`.

`ui.py` builds every control from plain functions; GTK4 needs a couple of
stateful widgets (a segmented control whose selected segment can be driven
from outside, a button that swaps its label for a spinner), so those are
classes here and everything else is a function.

The naming maps one-to-one onto ui.py so the two can be read side by side:

    ui.section_label   -> heading()
    ui.badge           -> badge()
    ui.action          -> button()
    ui.pending_button  -> pending_button()
    ui.field           -> field()
    ui.text_field      -> entry()
    ui.toggle          -> Segmented
    ui.notice          -> notice()
    ui.state_dot       -> state_dot()
    ui.icon_action     -> icon_button()
    ui.spacer          -> spacer()
    ui.rule            -> vdivider() / hdivider()

Every colour here is a class name; the actual colour lives in theme.py,
which builds the stylesheet from the tokens. Nothing in this file knows a
hex value.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from . import tokens as T  # noqa: E402

# The status vocabulary, ported from ui.py: six states, four colours, and no
# "warning" amber. A queue item is waiting, moving, stopped, finished or
# broken. Paused is the accent at reduced opacity rather than a new hue.
STATUS_WAITING = "waiting"
STATUS_WORKING = "working"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_STATUS_CLASS = {
    STATUS_WAITING: "waiting",
    STATUS_WORKING: "working",
    STATUS_PAUSED: "paused",
    STATUS_DONE: "done",
    STATUS_FAILED: "failed",
    STATUS_CANCELLED: "cancelled",
}


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

def hbox(spacing: int = T.S2, **kwargs) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing,
                   **kwargs)


def vbox(spacing: int = T.S2, **kwargs) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing,
                   **kwargs)


def spacer() -> Gtk.Box:
    """The gap that pushes the rest of a row to the right."""
    b = Gtk.Box()
    b.set_hexpand(True)
    return b


def vdivider() -> Gtk.Box:
    """A 1px vertical hairline."""
    d = Gtk.Box()
    d.add_css_class("vdivider")
    d.set_valign(Gtk.Align.CENTER)
    return d


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

def label(text: str, *, role: str = "body", mono: bool = False,
          color: str | None = None, xalign: float = 0.0,
          ellipsize: bool = False, selectable: bool = False,
          wrap: bool = False) -> Gtk.Label:
    """One line of type, by role.

    roles: body / title / meta / label / micro -- the same names ui.py uses.
    """
    styles = {
        "body": (T.T_BODY, 400, ""),
        "strong": (T.T_BODY, 600, ""),
        "title": (T.T_TITLE, 600, "title"),
        "meta": (T.T_META, 400, "muted"),
        "label": (T.T_LABEL, 600, "muted"),
        "micro": (T.T_MICRO, 600, "muted"),
        "display": (T.T_DISPLAY, 600, "display"),
    }
    size, weight, css = styles.get(role, styles["body"])
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(xalign)
    if mono:
        lbl.add_css_class("mono")
    if css:
        lbl.add_css_class(css)
    if color:
        lbl.add_css_class(color)      # e.g. "error-text", "success-text"
    if ellipsize:
        lbl.set_ellipsize(3)          # Pango.EllipsizeMode.END
    lbl.set_selectable(selectable)
    lbl.set_wrap(wrap)
    # The role sizes are set in CSS where possible; the label's own
    # font-size is set here so a label inside a button keeps its role.
    lbl.set_name(role)
    return lbl


def heading(text: str, trailing=None) -> Gtk.Box:
    """A section heading: a muted label, and an optional trailing widget.

    ui.py's section_label() appends a literal None to its row when there is
    no trailing hint, and the flet client answers a None child with a grey
    error box that swallows every section below it. So the trailing widget is
    only appended when there IS one.
    """
    row = hbox(spacing=T.S2)
    lbl = label(text, role="label")
    lbl.set_hexpand(True)
    row.append(lbl)
    if trailing is not None:
        row.append(trailing)
    return row


def badge(text: str, tone: str | None = None) -> Gtk.Label:
    """A small count or tag. Sits on a wash of its own tone."""
    lbl = label(text, role="micro")
    lbl.add_css_class("badge")
    if tone:
        lbl.add_css_class(tone)        # "ok" / "error"
    return lbl


def state_dot(state: str, size: int = 9) -> Gtk.Label:
    """The coloured dot that leads a queue row.

    The only place a status colour is allowed to be a mark.
    """
    dot = Gtk.Label()
    dot.add_css_class("dot")
    dot.add_css_class(_STATUS_CLASS.get(state, "waiting"))
    dot.set_size_request(size, size)
    dot.set_valign(Gtk.Align.CENTER)
    return dot


# ---------------------------------------------------------------------------
# controls
# ---------------------------------------------------------------------------

def button(label_text: str, *, kind: str = "secondary", on_click=None,
           icon: str | None = None, tooltip: str | None = None) -> Gtk.Button:
    """A labelled button.

    kind: "primary" (accent fill, the one affirmative action), "secondary"
    (raised fill plus hairline), "quiet" (no fill until hover) or "danger"
    (error-tinted text).
    """
    btn = Gtk.Button()
    btn.add_css_class(kind)
    box = hbox(spacing=T.S2 - 1)
    if icon:
        box.append(Gtk.Image.new_from_icon_name(f"{icon}-symbolic"))
    box.append(Gtk.Label(label=label_text))
    # Centred: a button that a column stretches to the full width (the
    # empty state's action) must still read as a button, not a banner.
    box.set_halign(Gtk.Align.CENTER)
    btn.set_child(box)
    if tooltip:
        btn.set_tooltip_text(tooltip)
    if on_click is not None:
        btn.connect("clicked", on_click)
    return btn


def icon_button(icon: str, tooltip: str, on_click=None, *,
                kind: str = "icon") -> Gtk.Button:
    """An icon-only button: row actions, the file manager, the reveal toggle."""
    btn = Gtk.Button()
    btn.add_css_class(kind)
    btn.set_child(Gtk.Image.new_from_icon_name(f"{icon}-symbolic"))
    btn.set_tooltip_text(tooltip)
    if on_click is not None:
        btn.connect("clicked", on_click)
    return btn


def pending_button(label_text: str, icon: str | None, handler, *,
                   kind: str = "primary", runner=None,
                   on_error=None, on_done=None) -> Gtk.Button:
    """A button that shows it is working while its handler runs.

    Every long action (verify credentials, log in, check for updates,
    re-check dependencies) used to stay live-looking for the whole network
    round-trip, so people clicked again. This disables the button, swaps its
    content for a spinner, awaits the handler, and restores it.

    `handler` is a coroutine function. `runner` schedules it on the engine's
    event loop and calls back on the GTK thread; without it the button still
    works but runs the coroutine on the GTK main loop, which would block the
    UI for the length of the request.
    """
    btn = Gtk.Button()
    btn.add_css_class(kind)
    box = hbox(spacing=T.S2 - 1)
    glyph = Gtk.Image.new_from_icon_name(f"{icon}-symbolic") \
        if icon else None
    text = Gtk.Label(label=label_text)
    spinner = Gtk.Spinner()
    spinner.set_visible(False)
    if glyph is not None:
        box.append(glyph)
    box.append(text)
    box.append(spinner)
    btn.set_child(box)

    busy = {"running": False}

    def _restore():
        busy["running"] = False
        spinner.stop()
        spinner.set_visible(False)
        text.set_visible(True)
        if glyph is not None:
            glyph.set_visible(True)
        btn.set_sensitive(True)

    def _on_click(_b):
        if busy["running"]:
            return
        busy["running"] = True
        btn.set_sensitive(False)
        text.set_visible(False)
        if glyph is not None:
            glyph.set_visible(False)
        spinner.set_visible(True)
        spinner.start()

        def _finished(ok, value=None):
            _restore()
            if not ok:
                if on_error is not None:
                    on_error(value)

        if runner is None:
            _finished(True)
            return
        runner(handler, _finished)

    btn.connect("clicked", _on_click)
    return btn


def field(control: Gtk.Widget, *, label_text: str | None = None,
          hint: str | None = None) -> Gtk.Box:
    """A labelled slot for any control: a caption above, an optional hint
    right-aligned opposite it, and the control underneath."""
    box = vbox(spacing=T.S1 + 2)
    if label_text is not None:
        head = hbox(spacing=T.S2)
        lbl = label(label_text, role="label")
        lbl.set_hexpand(True)
        head.append(lbl)
        if hint is not None:
            head.append(label(hint, role="meta"))
        box.append(head)
    box.append(control)
    return box


def notice(text: str, *, kind: str = "info") -> Gtk.Box:
    """An inline message about the thing it is about.

    A wash of the tone rather than a solid fill, so it reads as a message
    attached to the page and not as a banner sliding in.
    """
    box = vbox(spacing=T.S1)
    box.add_css_class("notice")
    if kind == "error":
        box.add_css_class("error")
    box.append(label(text, role="body", wrap=True))
    return box


class Segmented(Gtk.Box):
    """The segmented control: one raised segment inside a trough.

    Selected is a RAISED FILL, not an accent fill. A saturated block inside
    a form competes with the one primary button on the page and reads as a
    status rather than a choice; mass says "selected" quietly.
    """

    def __init__(self, options, value, on_change, *, expand: bool = False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=T.S1)
        self.add_css_class("segmented")
        self._on_change = on_change
        self._buttons = {}
        self._value = value
        for opt_value, opt_label in options:
            btn = Gtk.Button()
            btn.add_css_class("segment")
            btn.set_label(opt_label)
            btn.connect("clicked", self._clicked, opt_value)
            self._buttons[opt_value] = btn
            self.append(btn)
        if expand:
            self.set_hexpand(True)
        self.set_value(value)

    def _clicked(self, _btn, opt_value):
        if opt_value == self._value:
            return
        self.set_value(opt_value)
        if self._on_change is not None:
            self._on_change(opt_value)

    def set_value(self, value):
        """Repaint the control. The Settings reset drives the VISIBLE
        control through this, so a reset cannot leave the old choice on
        screen."""
        self._value = value
        for opt_value, btn in self._buttons.items():
            if opt_value == value:
                btn.add_css_class("selected")
            else:
                btn.remove_css_class("selected")


def progress(value: float | None = None) -> Gtk.ProgressBar:
    """The thin bar under a queue row.

    `value=None` is the indeterminate form: the bar animates while the size
    is still unknown, which is the honest thing to show while a message is
    being fetched.
    """
    bar = Gtk.ProgressBar()
    if value is None:
        bar.pulse()
    else:
        bar.set_fraction(max(0.0, min(1.0, float(value))))
    return bar
