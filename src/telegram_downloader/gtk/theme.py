"""The GTK4 stylesheet, built from the tokens.

GTK4 has no inline styles and no theme objects to subclass: a screen is
`Gtk.StyleContext.add_provider_for_display()` with a CSS string. So the
design system is expressed as a stylesheet whose colours are generated from
`tokens.py` -- the same numbers ui.py uses, not a second hand-tuned copy.

Two providers, one per mode. The theme toggle swaps the provider and flips
`gtk-application-prefer-dark-theme` (so the NATIVE widgets -- entries, the
file chooser, the scrollbars -- follow too); the tree is never rebuilt.

GTK4 CSS is a subset of CSS: no box-shadow (the design is tonal steps, not
drop shadows), no flexbox (GTK lays out with Box/Grid), and `alpha()` /
`rgba()` instead of `#RRGGBB,0.09`.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from . import tokens as T  # noqa: E402


def _rgba(value: str) -> str:
    """A token colour as a CSS colour.

    The palette stores opaque hex and the Flet-with-opacity form
    ("#FFFFFF,0.09"); GTK wants `rgba(r,g,b,a)`.
    """
    if "," not in value:
        return value
    hexpart, alpha = value.split(",", 1)
    hexpart = hexpart.lstrip("#")
    r, g, b = (int(hexpart[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{float(alpha):g})"


def _defines(p: dict) -> str:
    """`@define-color` lines for the tokens a rule refers to by name."""
    names = [
        ("page", "page"), ("well_deep", "well_deep"), ("well", "well"),
        ("raised", "raised"), ("floating", "floating"),
        ("outline", "outline"), ("edge", "edge"), ("hairline", "hairline"),
        ("ink", "ink"), ("muted", "muted"), ("faint", "faint"),
        ("accent", "accent"), ("accent_hover", "accent_hover"),
        ("on_accent", "on_accent"), ("accent_wash", "accent_wash"),
        ("success", "success"), ("error", "error"),
        ("error_wash", "error_wash"), ("success_wash", "success_wash"),
    ]
    out = []
    for css_name, key in names:
        out.append(f"@define-color {css_name} {_rgba(p[key])};")
    return "\n".join(out)


def css(dark: bool = True) -> str:
    """The whole stylesheet for one mode."""
    p = T.palette(dark)
    font = ", ".join(f'"{f}"' for f in [T.FONT_UI] + T.FONT_UI_STACK)
    mono = ", ".join(f'"{f}"' for f in [T.FONT_MONO] + T.FONT_MONO_STACK)
    return f"""
{_defines(p)}

/* ---------------------------------------------------------------- base --- */
window {{
  background-color: @page;
  color: @ink;
  font-family: {font};
  font-size: {T.T_BODY}px;
}}
label {{ color: @ink; }}
label.muted {{ color: @muted; }}
label.faint {{ color: @faint; }}
label.title {{ font-size: {T.T_TITLE}px; font-weight: 600; }}
label.display {{ font-size: {T.T_DISPLAY}px; font-weight: 600; }}
label.mono {{ font-family: {mono}; }}
label.error-text {{ color: @error; }}
label.success-text {{ color: @success; }}

/* ------------------------------------------------------------- the bars --- */
.topbar {{
  background-color: @page;
  border-bottom: 1px solid @hairline;
  padding: {T.S2}px {T.PAGE_PAD}px;
}}
.bottombar {{
  background-color: @raised;
  border-top: 1px solid @edge;
  padding: {T.S2}px {T.PAGE_PAD}px;
}}
.brand {{ font-weight: 600; }}
.vdivider {{
  background-color: @hairline;
  min-width: 1px;
  min-height: 18px;
  margin: 0 {T.S2}px 0 {T.S2}px;
}}
.page {{ padding: {T.S4}px {T.PAGE_PAD}px {T.S6}px {T.PAGE_PAD}px; }}

/* --------------------------------------------------------- the nav chips --- */
/* The selected destination is a RAISED surface with a hairline -- never an
   accent fill. Colour in this app means action or status, not "where am I",
   so the selected chip is the same material as the selected segment of a
   segmented control and nothing else. */
button.nav {{
  background-color: transparent;
  background-image: none;
  border: 1px solid transparent;
  border-radius: {T.R_SM}px;
  color: @muted;
  font-size: {T.T_BODY}px;
  font-weight: 500;
  padding: {T.S1 + 2}px {T.S3}px;
  min-height: 26px;
}}
button.nav:hover {{ color: @ink; background-color: alpha(@ink, 0.05); }}
button.nav:active {{ color: @ink; background-color: alpha(@ink, 0.09); }}
button.nav.selected {{
  background-color: @raised;
  border-color: @hairline;
  border-top-color: @edge;
  color: @ink;
  font-weight: 600;
}}
button.nav label {{ color: inherit; }}
button.nav.flat {{ border-color: transparent; }}

/* The theme toggle is chrome, not a destination: it gets air on its right so
   it never reads as a fifth tab. */
button.theme {{
  background-color: transparent;
  background-image: none;
  border: none;
  color: @muted;
  padding: {T.S1}px;
  min-width: 28px;
  min-height: 28px;
}}
button.theme:hover {{ color: @ink; background-color: alpha(@ink, 0.05); }}
button.theme label {{ color: inherit; }}

/* --------------------------------------------------------- the buttons --- */
button.primary {{
  background-color: @accent;
  background-image: none;
  border: none;
  border-radius: {T.R_MD}px;
  color: @on_accent;
  font-size: {T.T_BODY}px;
  font-weight: 600;
  padding: {T.S1 + 3}px {T.S3 + 2}px;
  min-height: 30px;
}}
button.primary:hover {{ background-color: @accent_hover; }}
button.primary:disabled {{ opacity: 0.45; }}
button.primary label {{ color: inherit; }}

button.secondary {{
  background-color: @raised;
  background-image: none;
  border: 1px solid @hairline;
  border-top-color: @edge;
  border-radius: {T.R_MD}px;
  color: @ink;
  font-size: {T.T_BODY}px;
  font-weight: 600;
  padding: {T.S1 + 3}px {T.S3 + 2}px;
  min-height: 30px;
}}
button.secondary:hover {{ background-color: @floating; }}
button.secondary:disabled {{ opacity: 0.45; }}
button.secondary label {{ color: inherit; }}

button.quiet {{
  background-color: transparent;
  background-image: none;
  border: none;
  border-radius: {T.R_MD}px;
  color: @muted;
  font-size: {T.T_BODY}px;
  font-weight: 600;
  padding: {T.S1 + 3}px {T.S3 + 2}px;
  min-height: 30px;
}}
button.quiet:hover {{ background-color: alpha(@ink, 0.05); color: @ink; }}
button.quiet label {{ color: inherit; }}

button.danger {{
  background-color: transparent;
  background-image: none;
  border: none;
  border-radius: {T.R_MD}px;
  color: @error;
  font-size: {T.T_BODY}px;
  font-weight: 600;
  padding: {T.S1 + 3}px {T.S3 + 2}px;
  min-height: 30px;
}}
button.danger:hover {{ background-color: @error_wash; }}
button.danger label {{ color: inherit; }}

/* An icon-only button: row actions, the theme toggle, the file manager. */
button.icon {{
  background-color: transparent;
  background-image: none;
  border: none;
  border-radius: {T.R_SM}px;
  color: @muted;
  padding: {T.S1}px;
  min-width: 28px;
  min-height: 28px;
}}
button.icon:hover {{ background-color: alpha(@ink, 0.05); color: @ink; }}
button.icon.danger {{ color: @error; }}
button.icon.danger:hover {{ background-color: @error_wash; }}
button.icon label {{ color: inherit; }}

/* ---------------------------------------------------------- the fields --- */
.field-label {{ font-size: {T.T_LABEL}px; font-weight: 600; color: @muted; }}
.field-hint {{ font-size: {T.T_META}px; color: @muted; }}
entry, textview {{
  background-color: @well_deep;
  color: @ink;
  border: 1px solid @outline;
  border-radius: {T.R_MD}px;
  padding: {T.S2 + 2}px {T.S3}px;
  font-size: {T.T_BODY}px;
  caret-color: @accent;
}}
entry:focus, textview:focus {{ border-color: @accent; }}
entry.error {{ border-color: @error; }}

/* --------------------------------------------------- segmented control --- */
/* The same material as the nav chips: a trough with a raised segment in it. */
.segmented {{
  background-color: @well;
  border-radius: {T.R_MD}px;
  padding: {T.S1}px;
}}
button.segment {{
  background-color: transparent;
  background-image: none;
  border: 1px solid transparent;
  border-radius: {T.R_SM}px;
  color: @muted;
  font-size: {T.T_BODY}px;
  font-weight: 400;
  padding: {T.S1}px {T.S3 + 2}px;
  min-height: 26px;
}}
button.segment:hover {{ color: @ink; }}
button.segment.selected {{
  background-color: @raised;
  border-color: @hairline;
  border-top-color: @edge;
  color: @ink;
  font-weight: 600;
}}
button.segment label {{ color: inherit; }}

/* ------------------------------------------------------------- badges --- */
.badge {{
  background-color: alpha(@muted, 0.12);
  color: @muted;
  border-radius: {T.R_SM}px;
  font-size: {T.T_MICRO}px;
  font-weight: 600;
  padding: 1px {T.S1 + 2}px;
}}
.badge.ok {{ background-color: alpha(@success, 0.12); color: @success; }}
.badge.error {{ background-color: alpha(@error, 0.12); color: @error; }}

/* ------------------------------------------------------------ notices --- */
.notice {{
  background-color: alpha(@accent, 0.10);
  border: 1px solid alpha(@accent, 0.35);
  border-radius: {T.R_MD}px;
  color: @ink;
  padding: {T.S2 + 2}px {T.S3}px;
}}
.notice.error {{
  background-color: alpha(@error, 0.10);
  border-color: alpha(@error, 0.35);
}}

/* ------------------------------------------------------ the list rows --- */
.row {{
  border-bottom: 1px solid @hairline;
  padding: {T.ROW_PAD_V}px {T.ROW_PAD_H}px;
}}
.row:hover {{ background-color: alpha(@ink, 0.05); }}
.list {{ background-color: transparent; }}

/* The status dot: a coloured mark, the only place a status colour lives. */
.dot {{
  background-color: @muted;
  border-radius: {T.R_FULL}px;
  min-width: 9px;
  min-height: 9px;
  padding: 0;
}}
.dot.working {{ background-color: @accent; }}
.dot.paused {{ background-color: alpha(@accent, 0.55); }}
.dot.done {{ background-color: @success; }}
.dot.failed {{ background-color: @error; }}
.dot.waiting {{ background-color: @muted; }}
.dot.cancelled {{ background-color: @outline; }}

/* -------------------------------------------------------- progress bar --- */
/* The node path is progressbar > trough > progress -- the fill lives INSIDE
   the trough, so `progressbar > progress` never matched and the bar came
   out in the theme's blue. background-image: none matters too: the theme
   paints the fill with a gradient image that a background-color does not
   replace. */
progressbar {{ min-height: 4px; }}
progressbar > trough {{
  background-color: @well_deep;
  background-image: none;
  border: none;
  border-radius: {T.R_FULL}px;
  min-height: 4px;
}}
progressbar > trough > progress {{
  background-color: @accent;
  background-image: none;
  border: none;
  border-radius: {T.R_FULL}px;
  min-height: 4px;
}}

/* The empty state's mark: a glyph in a disc, like ui.empty() draws it. */
.empty-mark {{
  background-color: @well;
  border-radius: {T.R_FULL}px;
  color: @outline;
}}

/* ------------------------------------------------------------- toasts --- */
.toast {{
  background-color: @floating;
  border: 1px solid @hairline;
  border-radius: {T.R_MD}px;
  color: @ink;
  padding: {T.S2 + 2}px {T.S3 + 2}px;
  margin: {T.S3}px;
}}
.toast.error {{ border-color: alpha(@error, 0.45); }}
.toast label {{ color: inherit; }}

/* ------------------------------------------------------------- dialogs --- */
dialog {{ background-color: @floating; color: @ink; }}
dialog label {{ color: inherit; }}
"""


# The provider currently installed, so it can be removed before the next one
# goes in. Gtk.StyleContext.remove_provider_for_display() needs the instance.
_current_provider = None


def apply(dark: bool = True) -> None:
    """Install the stylesheet for one mode.

    Swaps the provider rather than rebuilding the tree, and flips
    `gtk-application-prefer-dark-theme` so the NATIVE widgets (the file
    chooser, the scrollbars, the entry caret) follow the app's theme too.
    """
    global _current_provider
    display = Gdk.Display.get_default()
    if display is None:
        return
    if _current_provider is not None:
        # The STATIC form, called on the class. Gtk.StyleContext() -- the
        # deprecated instance -- SEGFAULTS in GTK 4.22 when a provider is
        # added through it.
        Gtk.StyleContext.remove_provider_for_display(display,
                                                     _current_provider)
    provider = Gtk.CssProvider()
    provider.load_from_data(css(dark).encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(display, provider, 1000)
    _current_provider = provider
    try:
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", dark)
    except Exception:
        pass
