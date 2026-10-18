"""Design language for the Telegram Downloader window.

Three ideas hold this together. Every view composes from them; nothing in the
GUI is allowed to invent a fourth.

1. ONE COLOUR FAMILY, ONE ACCENT. Every surface is graphite with a whisper of
   the accent's hue in it (chroma 0.011-0.013 at hue 250: enough that the greys
   are a family, not enough to read as navy). The accent is a cyan-aqua that
   sits one step warmer and lighter than Telegram's blue -- recognisably of the
   same family, plainly not the same colour. It is reserved for ACTION,
   PROGRESS and FOCUS: buttons, the progress bar, the focus ring. Never chrome.
   An earlier revision painted the top bar and the active tab with it, which
   turned the one accent in the app into a slab across the top of the window.

2. MATERIALS, NOT BOXES. There are five surface tones and each one means
   something: the page is the deepest tone, a well is *below* the page (things
   you put content into -- the link input, a progress trough, the nav strip),
   and raised / floating are *above* it (things that sit on top of content --
   the selected nav pill, the destination bar, a dialog). A list row gets no
   surface at all: rows are separated by a hairline, and only materials that
   genuinely float get a fill. Wrapping every list item in its own bordered
   card is what made this app read as boxes stacked on a page.

3. THE MEANING INVERTS CLEANLY. "Well" is always lower than the page and
   "raised" is always higher, in both themes. Material's own ramp flips
   direction between modes, so a role can render bright in one theme and dark
   in the other; that is a footgun, not a system, so the ramp here is
   monotonic in luminance in each theme and the docstring says which way.

Primitives never carry a literal colour. Each one is a Material role
(`ft.Colors.ON_SURFACE`) or an alpha derivative of one
(`with_opacity(0.09, ft.Colors.ON_SURFACE)`), so a control written next month
cannot be hard-coded to the wrong theme -- and a hairline composites over
whatever surface it lands on instead of assuming one.

Everything here is a plain function returning a Flet control. Flet 0.86.5
rebuilds UI by assigning `.controls` on live controls, and a class layer would
fight that; a function is also impossible to subclass into a second, slightly
different button, which is how toolbars drift.

Flet traps this module absorbs so the views do not have to:

* a `ft.Row` child with neither `expand=True` nor a fixed width renders at zero
  width, so `list_row` expands its middle column and every icon-and-label row
  here is built from width-carrying controls;
* `ft.TextField(expand=True)` under a scrolling `ft.Column` silently drops the
  whole subtree, so `field()` stretches its child instead of expanding it (and
  that combination is pinned in the module's own smoke test);
* `ft.CrossAxisAlignment.STRETCH` overrides an explicit child width, so widths
  are never set on a row that is also stretched.
"""
from __future__ import annotations

import flet as ft

# ---------------------------------------------------------------------------
# Palette
#
# Values are OKLCH conversions, not hand-picked hex, and each ramp is monotonic
# in lightness so "one step up" is one number. The ladders:
#
#   dark   well_deep .185  well .220  page .248  raised .295  floating .340
#   light  well_deep .928  well .952  page .978  raised 1.000 floating 1.000
#
# Light mode needs no floating step above white, so raised and floating are the
# same value there and depth comes from the shadow instead (see `surface`).
#
# Alpha hairlines ("#FFFFFF,0.09") are Flet's own `with_opacity` output format;
# they composite over the surface underneath, which is what makes one hairline
# token work on the page, on a raised bar and inside a dialog.
# ---------------------------------------------------------------------------

DARK = {
    # -- surfaces, deepest first
    "well_deep": "#0F1318",   # input fill, progress trough, code
    "well": "#161B20",        # nav strip, segmented-control trough
    "page": "#1D2227",        # the window itself
    "raised": "#282D32",      # selected nav pill, hovered row, bottom bar
    "floating": "#34383E",    # dialogs, sheets, menus, toasts
    # -- lines
    "hairline": "#FFFFFF,0.09",   # structural rule between rows and sections
    "outline": "#6E757D",         # control boundary: 3.44:1 on the page
    "edge": "#FFFFFF,0.14",       # 1px top highlight on a raised material
    # -- text
    "ink": "#F1F4F6",         # 14.5:1 on the page
    "muted": "#B4BBC3",       # 8.3:1 -- secondary text, status lines
    "faint": "#9199A2",       # 5.6:1 -- non-essential only, never the only label
    # -- accent (cyan-aqua: Telegram's blue family, one step warmer)
    "accent": "#30D7D9",      # 9.0:1 on the page
    "accent_hover": "#67EAEB",  # `primary_fixed`: hover/pressed for filled actions
    "on_accent": "#002428",   # 9.2:1 on the accent; the ink is blue-black, not pure black
    "accent_wash": "#043B3D",  # selected-row tint, active segment wash
    "on_accent_wash": "#86EBEC",
    # -- done (the only green in the app: a finished download)
    "success": "#62D286",     # 8.5:1 on the page
    "on_success": "#0A2312",
    "success_wash": "#163620",
    "on_success_wash": "#9FE9B2",
    # -- error (download failed, reset confirmed)
    "error": "#F25451",       # 4.7:1 on the page, 4.1:1 on a raised bar
    "on_error": "#311210",
    "error_wash": "#46221F",
    "on_error_wash": "#FFB9B1",
    # -- material plumbing
    "shadow": "#000000",
    "scrim": "#000000,0.60",
}

LIGHT = {
    "well_deep": "#E3E8EC",
    "well": "#ECEFF3",
    "page": "#F6F8FA",
    "raised": "#FFFFFF",
    "floating": "#FFFFFF",
    "hairline": "#000000,0.09",
    "outline": "#878D93",         # 3.15:1 on the page
    "edge": "#000000,0.06",       # a soft top edge; the shadow carries the rest
    "ink": "#1A1F24",             # 15.6:1
    "muted": "#545C64",           # 6.4:1
    "faint": "#7A8188",           # 3.7:1 -- non-essential only
    "accent": "#007A87",          # 4.8:1 on the page; white sits on it at 5.1:1
    "accent_hover": "#006572",
    "on_accent": "#FFFFFF",
    "accent_wash": "#C4F0F2",
    "on_accent_wash": "#005A64",
    "success": "#067E3F",         # 4.9:1
    "on_success": "#FFFFFF",
    "success_wash": "#D3F1D9",
    "on_success_wash": "#0A562B",
    "error": "#BE222A",           # 5.7:1
    "on_error": "#FFFFFF",
    "error_wash": "#FFE0DC",
    "on_error_wash": "#90101A",
    "shadow": "#000000",
    "scrim": "#000000,0.35",
}


def theme(dark: bool = True) -> ft.Theme:
    """The Flet theme for one mode. Dark is the one the app ships in.

    Every surface role the GUI paints is written out longhand, on purpose: this
    file is checked statically by scripts/check_flet_api.py, which cannot see
    through a `**dict` splat, so a ramp written as `**ramp` would reach a
    release build unverified. Every role the app paints is set here; nothing is
    left to Material to derive from the seed, because a derived surface is a
    colour nobody chose.
    """
    p = DARK if dark else LIGHT
    th = ft.Theme(
        color_scheme_seed=p["accent"],
        scaffold_bgcolor=p["page"],
        card_bgcolor=p["page"],
        font_family=FONT_UI,
        use_material3=True,
        visual_density=ft.VisualDensity.COMPACT,
        divider_color=p["hairline"],
        hint_color=p["faint"],
        disabled_color=p["faint"],
        canvas_color=p["page"],
        hover_color=ft.Colors.with_opacity(0.06, ft.Colors.ON_SURFACE),
        splash_color=ft.Colors.with_opacity(0.09, ft.Colors.ON_SURFACE),
        highlight_color=ft.Colors.with_opacity(0.07, ft.Colors.ON_SURFACE),
        focus_color=ft.Colors.with_opacity(0.20, ft.Colors.PRIMARY),
        color_scheme=ft.ColorScheme(
            # accent
            primary=p["accent"],
            on_primary=p["on_accent"],
            primary_container=p["accent_wash"],
            on_primary_container=p["on_accent_wash"],
            primary_fixed=p["accent_hover"],
            primary_fixed_dim=p["accent"],
            on_primary_fixed=p["on_accent"],
            on_primary_fixed_variant=p["on_accent"],
            # neutral support. `secondary` is the muted UI grey rather than a
            # second brand colour: any Material widget that reaches for it
            # (sliders, switches) should come out neutral, not violet.
            secondary=p["muted"],
            on_secondary=p["page"],
            secondary_container=p["raised"],
            on_secondary_container=p["ink"],
            secondary_fixed=p["raised"],
            secondary_fixed_dim=p["well"],
            on_secondary_fixed=p["ink"],
            on_secondary_fixed_variant=p["muted"],
            # done
            tertiary=p["success"],
            on_tertiary=p["on_success"],
            tertiary_container=p["success_wash"],
            on_tertiary_container=p["on_success_wash"],
            tertiary_fixed=p["success_wash"],
            tertiary_fixed_dim=p["success"],
            on_tertiary_fixed=p["on_success_wash"],
            on_tertiary_fixed_variant=p["on_success_wash"],
            # failed
            error=p["error"],
            on_error=p["on_error"],
            error_container=p["error_wash"],
            on_error_container=p["on_error_wash"],
            # surfaces. The ramp runs recessed -> page -> floating, and the
            # roles below are the only place those tones are named.
            surface=p["page"],
            on_surface=p["ink"],
            surface_dim=p["well"],
            surface_bright=p["floating"],
            on_surface_variant=p["muted"],
            surface_container_lowest=p["well_deep"],
            surface_container_low=p["well"],
            surface_container=p["page"],
            surface_container_high=p["raised"],
            surface_container_highest=p["floating"],
            # No `surface_variant`: flet 0.86.5's ColorScheme has no such
            # field. Flutter dropped it in favour of surface_container_highest,
            # which is set above. Adding it here would raise TypeError and
            # check_flet_api.py would flag it, so the trough tone is carried by
            # surface_container_low / _lowest and the two well_* tokens.
            surface_tint=p["accent"],
            outline=p["outline"],
            outline_variant=p["hairline"],
            inverse_surface=p["ink"],
            on_inverse_surface=p["page"],
            inverse_primary=p["on_accent_wash"],
            shadow=p["shadow"],
            scrim=p["scrim"],
        ),
        # Bare `ft.Icon(...)` should not arrive at Material's 24px: this is a
        # dense desktop app, and 16 is the size every explicit icon in the GUI
        # uses anyway.
        icon_theme=ft.IconTheme(size=16, color=p["muted"]),
        text_theme=ft.TextTheme(
            body_large=ft.TextStyle(size=T_BODY, color=p["ink"]),
            body_medium=ft.TextStyle(size=T_BODY, color=p["ink"]),
            body_small=ft.TextStyle(size=T_META, color=p["muted"]),
            label_large=ft.TextStyle(size=T_LABEL, weight=ft.FontWeight.W_600,
                                     color=p["muted"]),
            title_medium=ft.TextStyle(size=T_TITLE, weight=ft.FontWeight.W_600,
                                      color=p["ink"]),
        ),
        # Tooltips and menus get their own dark surface with a hairline rather
        # than Material's light "inverse" bubble, which looks like a system
        # dialog that wandered into this app.
        tooltip_theme=ft.TooltipTheme(
            decoration=ft.BoxDecoration(
                bgcolor=p["floating"],
                border_radius=ft.BorderRadius(8, 8, 8, 8),
                border=ft.Border.all(1, p["hairline"]),
            ),
            text_style=ft.TextStyle(size=T_META, color=p["ink"]),
            padding=ft.Padding(10, 6, 10, 6),
            wait_duration=400,
            prefer_below=False,
            vertical_offset=6,
        ),
        dialog_theme=ft.DialogTheme(
            bgcolor=p["floating"],
            elevation=2,
            shadow_color=p["shadow"],
            shape=ft.RoundedRectangleBorder(radius=R_LG),
            inset_padding=ft.Margin(24, 24, 24, 24),
            title_text_style=ft.TextStyle(size=T_TITLE, weight=ft.FontWeight.W_600,
                                          color=p["ink"]),
            content_text_style=ft.TextStyle(size=T_BODY, color=p["muted"]),
        ),
        bottom_sheet_theme=ft.BottomSheetTheme(
            bgcolor=p["floating"],
            elevation=2,
            shadow_color=p["shadow"],
            show_drag_handle=True,
            drag_handle_color=p["outline"],
            shape=ft.RoundedRectangleBorder(
                radius=ft.BorderRadius(top_left=R_LG, top_right=R_LG,
                                       bottom_left=0, bottom_right=0)),
        ),
        popup_menu_theme=ft.PopupMenuTheme(
            color=p["floating"],
            elevation=2,
            shadow_color=p["shadow"],
            label_text_style=ft.TextStyle(size=T_BODY, color=p["ink"]),
            shape=ft.RoundedRectangleBorder(radius=R_MD),
            menu_padding=ft.Padding(4, 4, 4, 4),
        ),
        snackbar_theme=ft.SnackBarTheme(
            bgcolor=p["floating"],
            content_text_style=ft.TextStyle(size=T_BODY, color=p["ink"]),
            action_text_color=p["accent"],
            close_icon_color=p["muted"],
            show_close_icon=True,
            elevation=2,
            behavior=ft.SnackBarBehavior.FLOATING,
            shape=ft.RoundedRectangleBorder(radius=R_MD),
        ),
        progress_indicator_theme=ft.ProgressIndicatorTheme(
            color=p["accent"],
            linear_track_color=p["well_deep"],
            linear_min_height=4,
            border_radius=R_FULL,
            circular_track_color=p["well"],
        ),
    )
    # Hidden at rest, shown while the pointer is on the scrollable. A permanent
    # groove is the loudest thing Flutter draws and it was the only visible
    # chrome in the whole window.
    th.scrollbar_theme = ft.ScrollbarTheme(
        thumb_visibility=False,
        track_visibility=False,
        thumb_color=ft.Colors.with_opacity(0.28, ft.Colors.ON_SURFACE),
        track_color=None,
        thickness=4,
        radius=2,
        min_thumb_length=40,
        interactive=True,
    )
    return th


# ---------------------------------------------------------------------------
# Type
#
# Six sizes, 10.5 to 14.5 in 1px steps plus one jump. The steps are deliberately
# small: the queue is a long list, and a 2px jump between a row title and its
# status line shouts where the eye only needs a nudge. 1px is the smallest step
# that still reads as a level, and weight (400/500/600) does the rest.
#
# 13.5 is the base because this is a dense desktop tool, not a document: 16px
# body is for reading prose, 13.5 is for scanning a list. The display step
# exists because the empty state is the one screen with a single thing on it.
# 10.5 is the smallest size in the app; below that digits smear at 1x.
# ---------------------------------------------------------------------------

T_MICRO = 10.5      # badges, chips
T_META = 11.5       # status lines, captions, field hints
T_LABEL = 12.5      # section labels, field labels
T_BODY = 13.5       # base: body copy, buttons, nav, inputs, values
T_TITLE = 14.5      # row titles, the wordmark, dialog titles
T_DISPLAY = 22      # empty-state headline: the only thing on screen

W_NORMAL = ft.FontWeight.W_400
W_MEDIUM = ft.FontWeight.W_500
W_SEMI = ft.FontWeight.W_600

# The app cannot ship a font (the package has no assets_dir and the .deb must
# stay dependency-light), so it asks for the desktop's own UI face and lists the
# rest of the family in order. An unknown name is skipped by the platform, so a
# machine with none of these gets its default sans -- never a blank glyph.
FONT_UI = "Cantarell"
FONT_UI_STACK = ["Ubuntu", "Noto Sans", "DejaVu Sans", "Liberation Sans",
                 "sans-serif"]
FONT_MONO = "DejaVu Sans Mono"
FONT_MONO_STACK = ["Ubuntu Mono", "Liberation Mono", "Noto Sans Mono",
                   "Consolas", "monospace"]

# role -> (size, weight, tone). A view asks for a ROLE, never a size: the sizes
# are the design system's business, and "make the metadata smaller" should be
# one edit here rather than thirty in the views.
ROLE_DISPLAY = "display"
ROLE_TITLE = "title"
ROLE_STRONG = "strong"
ROLE_BODY = "body"
ROLE_LABEL = "label"
ROLE_META = "meta"
ROLE_MICRO = "micro"

_ROLES = {
    ROLE_DISPLAY: (T_DISPLAY, W_SEMI, ft.Colors.ON_SURFACE),
    ROLE_TITLE: (T_TITLE, W_SEMI, ft.Colors.ON_SURFACE),
    ROLE_STRONG: (T_BODY, W_SEMI, ft.Colors.ON_SURFACE),
    ROLE_BODY: (T_BODY, W_NORMAL, ft.Colors.ON_SURFACE),
    ROLE_LABEL: (T_LABEL, W_SEMI, ft.Colors.ON_SURFACE_VARIANT),
    ROLE_META: (T_META, W_NORMAL, ft.Colors.ON_SURFACE_VARIANT),
    ROLE_MICRO: (T_MICRO, W_SEMI, ft.Colors.ON_SURFACE_VARIANT),
}


# ---------------------------------------------------------------------------
# Space, radius, motion
#
# Spacing is 4-based with an 8 anchor, used in two tiers: 4/8/12 inside a
# component (icon to label, row padding), 16/24/32 between blocks. The 4px half
# step exists because a desktop row needs a gap tighter than 8 or the icon
# floats away from its label.
# ---------------------------------------------------------------------------

S1 = 4
S2 = 8
S3 = 12
S4 = 16
S5 = 20
S6 = 24
S7 = 32
S8 = 40

PAGE_PAD = 24       # window gutter: content runs full width inside it
BAR_H = 52          # the top bar
ROW_PAD_H = 12      # inside a list row
ROW_PAD_V = 10

# Three radii with visible separation (6 / 10 / 16), scaled to the object they
# are on: a control inside a row, a panel or button, then a floating sheet. The
# size step between them is ~1.6x, so "rounder" reads as a level; the previous
# set (7/8/12) differed by a pixel and read as sloppiness instead.
R_SM = 6            # chips, icon buttons, segments
R_MD = 10           # buttons, inputs, rows with a fill, menus
R_LG = 16           # sheets, dialogs, and the logo tile
R_FULL = 999        # pills, dots, progress bars

# Motion is for state changes, never for layout: colour and opacity only. A
# control that animates its own size re-lays-out its row on every frame, which
# is the difference between a desktop app and a web page having a bad day.
DUR_FAST = 120
DUR = 170
DUR_SLOW = 260
ANIM_FAST = ft.Animation(duration=DUR_FAST, curve=ft.AnimationCurve.EASE_OUT_CUBIC)
ANIM = ft.Animation(duration=DUR, curve=ft.AnimationCurve.EASE_OUT_CUBIC)
ANIM_SLOW = ft.Animation(duration=DUR_SLOW, curve=ft.AnimationCurve.EASE_OUT_CUBIC)


# ---------------------------------------------------------------------------
# Status vocabulary
#
# Six states, four colours, and no "warning" amber: a queue item is waiting,
# moving, stopped, finished or broken. Paused is the accent at reduced opacity
# rather than a new hue -- it IS a working item, currently not moving, and
# inventing a fifth colour for it would mean the palette has to explain amber
# on dark, where Material has no warning role to give it contrast in both
# themes. Idle states are grey; that is what grey is for.
#
# Every mark colour clears 3:1 against every surface tone in its own theme, and
# the text colours clear 4.5:1, which is why status text may be coloured at all.
# ---------------------------------------------------------------------------

STATUS_WAITING = "waiting"
STATUS_WORKING = "working"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUS = {
    STATUS_WAITING: (ft.Colors.ON_SURFACE_VARIANT, "Waiting"),
    STATUS_WORKING: (ft.Colors.PRIMARY, "Downloading"),
    STATUS_PAUSED: (ft.Colors.with_opacity(0.55, ft.Colors.PRIMARY), "Paused"),
    STATUS_DONE: (ft.Colors.TERTIARY, "Done"),
    STATUS_FAILED: (ft.Colors.ERROR, "Failed"),
    STATUS_CANCELLED: (ft.Colors.OUTLINE, "Cancelled"),
}


def status_color(state: str) -> str:
    """The mark colour for a queue state. Unknown states read as waiting."""
    return STATUS.get(state, STATUS[STATUS_WAITING])[0]


def status_label(state: str) -> str:
    """The default wording for a queue state; views may pass their own line."""
    return STATUS.get(state, STATUS[STATUS_WAITING])[1]


# Tones the primitives speak in, all as Material roles so they follow the theme
# at render time rather than at import time.
TONE_INK = ft.Colors.ON_SURFACE
TONE_MUTED = ft.Colors.ON_SURFACE_VARIANT


def _alpha(fraction: float, color) -> str:
    """A translucent version of a role. Used for hairlines and washes."""
    return ft.Colors.with_opacity(fraction, color)


HAIRLINE = _alpha(0.09, ft.Colors.ON_SURFACE)        # rules between rows
EDGE = _alpha(0.14, ft.Colors.ON_SURFACE)            # top edge of a raised fill
HOVER = _alpha(0.05, ft.Colors.ON_SURFACE)           # pointer-over a row
PRESSED = _alpha(0.09, ft.Colors.ON_SURFACE)
SELECTED = _alpha(0.09, ft.Colors.PRIMARY)           # the one selected row
FOCUS = ft.Colors.PRIMARY


def _radius(px: int) -> ft.BorderRadius:
    return ft.BorderRadius(px, px, px, px)


def _pad(l: int, t: int, r: int, b: int) -> ft.Padding:
    return ft.Padding(l, t, r, b)


def _hovered(event) -> bool:
    """Flet reports hover as a bool in some builds and 'true' in others."""
    return event.data is True or str(event.data).lower() == "true"


# ---------------------------------------------------------------------------
# Primitives: structure
# ---------------------------------------------------------------------------

def stack(controls, *, spacing: int = S2, expand: bool = False,
          scroll=None, stretch: bool = False, fill: bool = False,
          padding=None):
    """A vertical run of controls.

    Two different things are often both called "full size", and mixing them up
    is what puts a giant blank block on screen:

    `expand=True`  the run GROWS into whatever space its parent has left. Right
                   for a page body or a pane that owns a region.
    `fill=True`    the run spans the cross axis but stays as tall as its
                   content. Right for a full-width block in a scrolling page.
    neither        the run shrink-wraps. Right inside another row.

    `expand` also clears Flet's `tight`, which a Column defaults to ON: a tight
    Column inside a scrollable parent measures itself against infinite height
    and stretches. `stretch` is for the one case where a child must fill the
    cross axis (an input under its label) -- and note that STRETCH overrides a
    child's explicit width, so never pass both.

    `padding` takes an int (all four sides) or an `ft.Padding`. Flet's Column
    has no `padding` field, so a non-None value wraps the run in a container.
    It is here so a page body can be inset without being wrapped in a
    `surface`, which would change its colour to buy a margin.
    """
    column = ft.Column(
        list(controls),
        spacing=spacing,
        tight=not (expand or fill),
        expand=expand,
        scroll=scroll,
        horizontal_alignment=(ft.CrossAxisAlignment.STRETCH if stretch
                              else ft.CrossAxisAlignment.START),
    )
    return _inset(column, padding, expand)


def cluster(controls, *, spacing: int = S2, expand: bool = False,
            fill: bool = False, wrap: bool = False, top: bool = False,
            padding=None):
    """A horizontal run of controls: a toolbar, an action row, a title line.

    `fill=True` spans the parent's width while keeping the row's own height --
    what a full-width bar, section label or settings line wants. `expand=True`
    additionally grows into the parent's leftover space, and in a scrolling
    parent that space is infinite, so reserve it for a row that genuinely owns
    a region (the nav strip inside the top bar) and never use it for a line of
    text: the row will balloon and push everything below it off the screen.

    Every child keeps its own intrinsic width, which means every child must
    HAVE one. Text and icons do; a bare control that needs to stretch must be
    wrapped with `expand=True` by the caller, or given a width.
    """
    return _inset(
        ft.Row(
            list(controls),
            spacing=spacing,
            tight=not (expand or fill),
            expand=expand,
            wrap=wrap,
            vertical_alignment=(ft.CrossAxisAlignment.START if top
                                else ft.CrossAxisAlignment.CENTER),
        ),
        padding, expand,
    )


def _inset(control, padding, expand: bool = False):
    """Wrap `control` in a padded container, or hand it back untouched.

    Flet's Row and Column have `margin` but no `padding`, so an inset run is
    the only place a wrapper is needed. Returning the bare control when there
    is no padding keeps the widget tree the depth the views expect.
    """
    if padding is None:
        return control
    return ft.Container(content=control, padding=padding, expand=expand)


def spacer() -> ft.Container:
    """The gap that pushes the rest of a row to the right.

    A `ft.Container(expand=True)` and nothing else. It exists so views stop
    hand-writing it and get the same behaviour everywhere.
    """
    return ft.Container(expand=True)


def rule(*, vertical: bool = False, color: str | None = None,
         thickness: int = 1) -> ft.Container:
    """A 1px hairline. The only separator in the app.

    Defaults to a white-alpha (or black-alpha) line so it composites over
    whatever surface it is on instead of assuming the page.
    """
    line = color or HAIRLINE
    return ft.Container(
        width=thickness if vertical else None,
        height=None if vertical else thickness,
        bgcolor=line,
        expand=not vertical,
    ) if vertical else ft.Container(height=thickness, bgcolor=line)


def surface(body=None, *, tone: str = "page", pad: int | None = S3,
            radius: int = R_MD, expand: bool = False, on_click=None,
            tooltip: str | None = None, width: int | None = None,
            animate: bool = False):
    """The material primitive. Every filled area in the app comes from here.

    tone:
      "page"     the window tone; for a container that just needs a colour
      "well"     BELOW the page: the link input, a progress trough, a code block
      "trough"   below the page, one step up from a well: the nav strip, the
                 segmented-control base
      "raised"   ABOVE the page: the selected nav pill, a hovered row, the
                 destination bar. Carries a 1px lighter top edge, which is what
                 makes a tonal step read as a raised surface rather than a
                 slightly different grey.
      "floating" ABOVE everything: dialogs, sheets, menus, toasts
      "plain"    no fill at all, for a structural wrapper that only needs
                 padding, a radius or a click target

    There is no "card". A list item is a row with a hover wash, not a box.
    """
    fills = {
        "page": ft.Colors.SURFACE,
        "well": ft.Colors.SURFACE_CONTAINER_LOWEST,
        "trough": ft.Colors.SURFACE_CONTAINER_LOW,
        "raised": ft.Colors.SURFACE_CONTAINER_HIGH,
        "floating": ft.Colors.SURFACE_CONTAINER_HIGHEST,
        "plain": None,
    }
    if tone not in fills:
        raise ValueError(f"unknown surface tone {tone!r}")
    border = None
    if tone in ("raised", "floating"):
        # Lighter on top, hairline elsewhere: the cheapest way to say "this
        # sits above the page" without a drop shadow, which on a dark theme
        # mostly reads as dirt.
        border = ft.Border(
            top=ft.BorderSide(1, EDGE),
            left=ft.BorderSide(1, HAIRLINE),
            right=ft.BorderSide(1, HAIRLINE),
            bottom=ft.BorderSide(1, HAIRLINE),
        )
    ctl = ft.Container(
        content=body,
        bgcolor=fills[tone],
        border=border,
        border_radius=_radius(radius),
        padding=_pad(pad, pad, pad, pad) if pad is not None else None,
        expand=expand,
        width=width,
        tooltip=tooltip,
        on_click=on_click,
        ink=on_click is not None,
        animate=ANIM_FAST if animate else None,
    )
    return ctl


def bar(controls, *, tone: str | None = None, rule_below: bool = False,
        pad_x: int = PAGE_PAD, pad_y: int = S2, height: int | None = None):
    """A full-width horizontal strip: the top bar, the destination bar, a
    toolbar. `rule_below` gives it the hairline that separates chrome from
    content -- one line instead of a second background colour.

    Takes a list (wrapped in a spread row) or a control you have already laid
    out yourself.
    """
    inner = (cluster(controls, spacing=S2, fill=True)
             if isinstance(controls, (list, tuple)) else controls)
    fills = {
        None: None,
        "page": ft.Colors.SURFACE,
        "raised": ft.Colors.SURFACE_CONTAINER_HIGH,
        "floating": ft.Colors.SURFACE_CONTAINER_HIGHEST,
        "trough": ft.Colors.SURFACE_CONTAINER_LOW,
    }
    if tone not in fills:
        raise ValueError(f"unknown bar tone {tone!r}")
    return ft.Container(
        content=inner,
        bgcolor=fills[tone],
        height=height,
        padding=_pad(pad_x, pad_y, pad_x, pad_y),
        border=(ft.Border(bottom=ft.BorderSide(1, HAIRLINE)) if rule_below
                else None),
    )


# ---------------------------------------------------------------------------
# Primitives: text
# ---------------------------------------------------------------------------

def text(value, *, role: str = ROLE_BODY, color=None, mono: bool = False,
         weight=None, size=None, selectable: bool = False,
         max_lines: int | None = None, overflow=None, align=None,
         expand: bool = False, no_wrap: bool = False):
    """One line of type, by ROLE.

    roles: display / title / strong / body / label / meta / micro.
    `strong` is body size at 600 -- for a value that must not be missed without
    being a heading. `mono` switches the face for machine text (paths, versions,
    api ids), which is the only place a second family appears.
    """
    spec = _ROLES.get(role, _ROLES[ROLE_BODY])
    return ft.Text(
        value,
        size=size or spec[0],
        weight=weight or spec[1],
        color=color or spec[2],
        # `font_family_fallback` only exists on Text/TextStyle, not on Theme,
        # which is why the stack lives here and the theme carries the head of it.
        font_family=(FONT_MONO if mono else FONT_UI),
        font_family_fallback=(FONT_MONO_STACK if mono else FONT_UI_STACK),
        selectable=selectable,
        max_lines=max_lines,
        overflow=overflow,
        text_align=align,
        no_wrap=no_wrap,
        expand=expand,
    )


def section_label(value, *, trailing=None):
    """A muted section heading with no box around it, plus an optional
    right-aligned hint. Sections are told apart by one of these and a hairline;
    that is the whole of the Settings page's structure."""
    return cluster(
        [text(value, role=ROLE_LABEL), spacer(),
         trailing if trailing is None or isinstance(trailing, ft.Control)
         else text(trailing, role=ROLE_META)],
        spacing=S2, fill=True,
    )


def key_value(key, value, *, mono: bool = False):
    """A settings/info line: muted label, value pushed right. `value` may be a
    control when it is not plain text (a path with a trailing icon, say)."""
    return cluster(
        [text(key, role=ROLE_BODY), spacer(),
         value if isinstance(value, ft.Control)
         else text(value, role=ROLE_STRONG, mono=mono, selectable=mono)],
        spacing=S3, fill=True,
    )


# ---------------------------------------------------------------------------
# Primitives: marks
# ---------------------------------------------------------------------------

def state_dot(state: str, *, size: int = 9):
    """The coloured dot that leads a queue row. The only place a status colour
    is allowed to be a mark."""
    return ft.Container(
        width=size, height=size,
        border_radius=R_FULL,
        bgcolor=status_color(state),
    )


def badge(value, *, tone=None):
    """A small count or tag. Sits on a wash of its own tone so it reads as a
    chip without becoming a filled block."""
    tint = tone or ft.Colors.ON_SURFACE_VARIANT
    return ft.Container(
        content=ft.Text(
            str(value), size=T_MICRO, weight=W_SEMI, color=tint,
            font_family=FONT_UI, font_family_fallback=FONT_UI_STACK,
        ),
        bgcolor=_alpha(0.12, tint),
        border_radius=R_SM,
        padding=_pad(6, 1, 6, 1),
    )


def status_line(state: str, label: str | None = None, *, coloured: bool = False,
                icon=None):
    """Status as text, next to a dot. Text stays muted by default: a list of
    rows with coloured sentences in it is a list nobody can scan. `coloured`
    is for the one place where the sentence IS the message (a failed row).
    """
    lead = icon if icon is not None else state_dot(state, size=7)
    return cluster(
        [lead,
         text(label or status_label(state), role=ROLE_META,
              color=(status_color(state) if coloured else TONE_MUTED))],
        spacing=S2 if isinstance(lead, ft.Icon) else S1 + 2,
    )


def progress(value: float | None, *, state: str = STATUS_WORKING,
             height: int = 4, expand: bool = True):
    """The thin bar under a queue row.

    `value=None` is the indeterminate form: the bar animates while the size is
    still unknown, which is the honest thing to show while a message is being
    fetched. The trough is the deepest tone in the ramp, so the bar reads as
    sunk into the row rather than painted on it.
    """
    if value is not None:
        value = max(0.0, min(1.0, float(value)))
    return ft.ProgressBar(
        value=value,
        bar_height=height,
        border_radius=R_FULL,
        color=status_color(state),
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
        expand=expand,
    )


def spinner(*, size: int = 14, color=None):
    """Inline busy ring, for work that is not a queue item (logging in,
    checking a link)."""
    return ft.ProgressRing(
        width=size, height=size, stroke_width=2,
        color=color or ft.Colors.PRIMARY,
        bgcolor=ft.Colors.TRANSPARENT,
    )


# ---------------------------------------------------------------------------
# Primitives: controls
#
# Five states, every time: resting, hover, focus-visible, pressed, disabled.
# Nothing here is missing one, and nothing here invents a sixth look.
# ---------------------------------------------------------------------------

def action(label, icon=None, on_click=None, *, kind: str = "primary",
           disabled: bool = False, expand: bool = False,
           tooltip: str | None = None, width: int | None = None):
    """A labelled button.

    kind:
      "primary"    accent fill. One per screen, at most: the thing the user
                   came here to do. Hover uses `primary_fixed`, the one step
                   lighter accent, so the state change is a real tonal shift.
      "secondary"  raised fill plus hairline. Everything else that does
                   something.
      "quiet"      no fill until hover. Toolbars and row-level actions.
      "danger"     error-tinted text; fills with the error wash on hover. For
                   the destructive end of a dialog, use "primary" with the
                   error wording in the label.
    """
    tones = {
        "primary": (ft.Colors.PRIMARY, ft.Colors.ON_PRIMARY, ft.Colors.PRIMARY_FIXED),
        "secondary": (ft.Colors.SURFACE_CONTAINER_HIGH, ft.Colors.ON_SURFACE, None),
        "quiet": (None, ft.Colors.ON_SURFACE_VARIANT, ft.Colors.SURFACE_CONTAINER_HIGH),
        "danger": (None, ft.Colors.ERROR, _alpha(0.14, ft.Colors.ERROR)),
    }
    if kind not in tones:
        raise ValueError(f"unknown action kind {kind!r}")
    fill, fg, hover_fill = tones[kind]
    if kind == "primary":
        fg = ft.Colors.ON_PRIMARY

    body = cluster(
        ([ft.Icon(icon, size=15, color=fg)] if icon is not None else [])
        + [ft.Text(label, size=T_BODY, weight=W_SEMI, color=fg,
                   font_family=FONT_UI, font_family_fallback=FONT_UI_STACK)],
        spacing=S2 - 1,
    )
    ctl = ft.Container(
        content=body,
        bgcolor=fill,
        border=(ft.Border(
            top=ft.BorderSide(1, EDGE),
            left=ft.BorderSide(1, HAIRLINE),
            right=ft.BorderSide(1, HAIRLINE),
            bottom=ft.BorderSide(1, HAIRLINE),
        ) if kind == "secondary" else None),
        border_radius=_radius(R_MD),
        padding=_pad(14, 7, 14, 7),
        width=width,
        expand=expand,
        alignment=ft.Alignment.CENTER,
        tooltip=tooltip,
        opacity=0.45 if disabled else 1,
        on_click=None if disabled else on_click,
        on_hover=None if (disabled or on_click is None) else _on_hover_fill(
            hover_fill, fill),
        ink=not disabled and on_click is not None,
        animate=ANIM_FAST,
    )
    return ctl


def _on_hover_fill(hover_fill, resting_fill):
    def handler(event):
        event.control.bgcolor = hover_fill if _hovered(event) else resting_fill
        try:
            event.control.update()
        except Exception:
            pass
    return handler


def icon_action(icon, tooltip: str, on_click=None, *, kind: str = "quiet",
                disabled: bool = False, size: int = 15, box: int = 30,
                color=None):
    """An icon-only button: row actions, the theme toggle, the header cluster.

    `kind` is the same vocabulary as `action`: "quiet" for row actions,
    "danger" for cancel/remove, "accent" for the one affirmative icon action.
    `box` is the click target, not the glyph: 30px keeps a toolbar dense while
    staying a comfortable pointer target next to 13.5px text.
    """
    tones = {
        "quiet": (None, ft.Colors.ON_SURFACE_VARIANT, TONE_INK),
        "accent": (None, ft.Colors.PRIMARY, ft.Colors.PRIMARY),
        "danger": (None, ft.Colors.ERROR, ft.Colors.ERROR),
    }
    if kind not in tones:
        raise ValueError(f"unknown icon action kind {kind!r}")
    fill, fg, hover_fg = tones[kind]
    fg = color or fg
    glyph = ft.Icon(icon, size=size, color=fg)

    def on_hover(event):
        over = _hovered(event)
        event.control.bgcolor = HOVER if over else fill
        glyph.color = hover_fg if over else fg
        try:
            event.control.update()
        except Exception:
            pass

    return ft.Container(
        content=glyph,
        width=box, height=box,
        alignment=ft.Alignment.CENTER,
        border_radius=_radius(R_SM),
        bgcolor=fill,
        tooltip=tooltip,
        opacity=0.40 if disabled else 1,
        on_click=None if disabled else on_click,
        on_hover=None if disabled else on_hover,
        ink=not disabled,
        animate=ANIM_FAST,
    )


def _option_pairs(options):
    """Normalise segmented-control options to (value, label) pairs.

    A flat list of labels is the common case -- retry counts, quality tiers --
    and without this the obvious `for value, label in ["3", "5"]` unpacks the
    strings character by character and dies with a confusing "not enough values
    to unpack" from inside the primitive. Accept both shapes.
    """
    pairs = []
    for option in options:
        if isinstance(option, str):
            pairs.append((option, option))
            continue
        try:
            option_value, label = option
        except (TypeError, ValueError):
            raise ValueError(
                f"toggle option must be a label or a (value, label) pair, "
                f"not {option!r}") from None
        pairs.append((option_value, label))
    return pairs


def toggle(options, value, on_change, *, expand: bool = False):
    """A segmented control: one raised segment inside a trough.

    Selected is a RAISED FILL, not an accent fill. A saturated block inside a
    settings form competes with the one primary button on the page and reads as
    a status rather than a choice; mass says "selected" quietly.

    `options` is either a list of labels, where the label is also the value
    (["3", "5", "Never"]), or a list of (value, label) pairs when the two
    differ ([("never", "Never")]). Returns a container; the view keeps
    ownership of the state it passes in.
    """
    state = {"value": value}
    pairs = _option_pairs(options)

    def build():
        segments = []
        for option_value, label in pairs:
            selected = state["value"] == option_value
            segments.append(ft.Container(
                content=ft.Text(
                    label, size=T_BODY,
                    weight=W_SEMI if selected else W_NORMAL,
                    color=(ft.Colors.ON_SURFACE if selected
                           else ft.Colors.ON_SURFACE_VARIANT),
                    font_family=FONT_UI, font_family_fallback=FONT_UI_STACK,
                ),
                bgcolor=(ft.Colors.SURFACE_CONTAINER_HIGH if selected else None),
                border=(ft.Border(
                    top=ft.BorderSide(1, EDGE),
                    left=ft.BorderSide(1, HAIRLINE),
                    right=ft.BorderSide(1, HAIRLINE),
                    bottom=ft.BorderSide(1, HAIRLINE),
                ) if selected else None),
                padding=_pad(14, 5, 14, 5),
                border_radius=_radius(R_SM),
                on_click=(None if selected else
                          (lambda e, v=option_value: _pick(v))),
                ink=not selected,
                animate=ANIM_FAST,
                data=option_value,
            ))
        return segments

    def _pick(new_value):
        state["value"] = new_value
        row = holder.content
        row.controls = build()
        try:
            row.update()
        except Exception:
            pass
        on_change(new_value)

    holder = ft.Container(
        content=cluster(build(), spacing=S1),
        expand=expand,
    )
    trough = surface(holder, tone="trough", pad=S1, radius=R_MD, expand=expand)
    return trough


def nav_tab(label, *, selected: bool, on_click=None, leading=None,
            badge_value=None):
    """One tab of the top bar.

    The navigation is the same segmented control as the rest of the app: the
    tab you are on is a raised pill. There is no accent underline -- colour in
    this app means action or state, and "where am I" is neither.
    """
    parts = []
    if leading is not None:
        parts.append(ft.Icon(
            leading, size=15,
            color=ft.Colors.ON_SURFACE if selected else ft.Colors.ON_SURFACE_VARIANT,
        ))
    parts.append(ft.Text(
        label, size=T_BODY,
        weight=W_SEMI if selected else W_MEDIUM,
        color=ft.Colors.ON_SURFACE if selected else ft.Colors.ON_SURFACE_VARIANT,
        font_family=FONT_UI, font_family_fallback=FONT_UI_STACK,
    ))
    if badge_value is not None:
        parts.append(badge(badge_value))
    return ft.Container(
        content=cluster(parts, spacing=S2 - 1),
        bgcolor=(ft.Colors.SURFACE_CONTAINER_HIGH if selected else None),
        border=(ft.Border(
            top=ft.BorderSide(1, EDGE),
            left=ft.BorderSide(1, HAIRLINE),
            right=ft.BorderSide(1, HAIRLINE),
            bottom=ft.BorderSide(1, HAIRLINE),
        ) if selected else None),
        border_radius=_radius(R_SM),
        padding=_pad(12, 6, 12, 6),
        on_click=on_click,
        ink=not selected,
        animate=ANIM_FAST,
        data=label,
    )


def nav(controls, *, expand: bool = False):
    """The trough the nav tabs sit in. Same material as `toggle`, so the tab
    strip and the three-way output-mode control are visibly the same thing."""
    return surface(cluster(list(controls), spacing=S1, expand=expand),
                   tone="trough", pad=S1, radius=R_MD, expand=expand)


def text_field(value=None, *, hint: str | None = None, on_change=None,
               on_submit=None, password: bool = False, reveal: bool = False,
               multiline: bool = False, min_lines: int | None = None,
               max_lines: int | None = None, disabled: bool = False,
               autofocus: bool = False, width: int | None = None,
               dense: bool = True, size: int = T_BODY):
    """A text input. Fill is the deepest tone, border is the hairline role and
    focus is the accent -- the only three things an input needs to say.

    Deliberately does NOT set `expand`: a `ft.TextField(expand=True)` under a
    scrolling `ft.Column` takes its whole subtree down with it in flet 0.86.5.
    Put it in `field()` to make it fill the width.
    """
    return ft.TextField(
        value=value,
        hint_text=hint,
        on_change=on_change,
        on_submit=on_submit,
        password=password,
        can_reveal_password=reveal,
        multiline=multiline,
        min_lines=min_lines,
        max_lines=max_lines,
        disabled=disabled,
        autofocus=autofocus,
        dense=dense,
        width=width,
        filled=True,
        fill_color=ft.Colors.SURFACE_CONTAINER_LOWEST,
        border_color=ft.Colors.OUTLINE_VARIANT,
        focused_border_color=ft.Colors.PRIMARY,
        border_width=1,
        focused_border_width=2,
        border_radius=_radius(R_MD),
        text_size=size,
        color=ft.Colors.ON_SURFACE,
        cursor_color=ft.Colors.PRIMARY,
        cursor_height=18,
        selection_color=_alpha(0.30, ft.Colors.PRIMARY),
        hint_style=ft.TextStyle(size=size, color=ft.Colors.ON_SURFACE_VARIANT),
        content_padding=_pad(12, 10, 12, 10),
        text_style=ft.TextStyle(
            size=size, color=ft.Colors.ON_SURFACE,
            font_family=FONT_UI, font_family_fallback=FONT_UI_STACK,
        ),
    )


def field(control, *, label: str | None = None, hint: str | None = None):
    """A labelled slot for any control: a caption above, an optional hint
    right-aligned opposite it, and the control stretched across the width.

    STRETCH is used here rather than `expand=True` on the child, which is the
    combination that survives being inside a scrolling Column (see
    `text_field`). One consequence worth knowing: a stretched child ignores its
    own width, so do not pass a width to a control you put in here.
    """
    head = []
    if label is not None:
        head.append(text(label, role=ROLE_LABEL))
    if hint is not None:
        head.append(spacer())
        head.append(text(hint, role=ROLE_META, mono=True))
    return ft.Column(
        ([cluster(head, spacing=S2, fill=True)] if head else []) + [control],
        spacing=S1 + 2,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


# ---------------------------------------------------------------------------
# Primitives: composed
# ---------------------------------------------------------------------------

def list_row(*, leading=None, title=None, meta=None, trailing=None,
             progress_value=None, state: str | None = None,
             on_click=None, selected: bool = False):
    """One queue item -- the app's most-repeated component.

    No fill, no border: a row is a line of the list, not a card. Hover is a 5%
    ink wash, selection is a 9% accent wash, and the whole row is the click
    target. The middle column is the only thing that expands, which is also
    what stops flet from rendering it at zero width.

    `state` colours the leading dot and the progress bar; pass `leading` to
    replace the dot with something else (a number, an icon).
    """
    middle = []
    if title is not None:
        middle.append(title if isinstance(title, ft.Control)
                      else text(title, role=ROLE_TITLE, max_lines=1))
    if meta is not None:
        middle.append(meta if isinstance(meta, ft.Control)
                      else text(meta, role=ROLE_META, max_lines=1))
    if progress_value is not None or state == STATUS_WORKING:
        middle.append(progress(progress_value,
                               state=state or STATUS_WORKING))
    column = ft.Column(
        middle,
        spacing=S1 + 1,
        tight=True,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    line = []
    if leading is not None:
        line.append(leading)
    elif state is not None:
        line.append(ft.Container(content=state_dot(state),
                                 alignment=ft.Alignment.CENTER, width=10))
    line.append(column)
    if trailing is not None:
        line.append(trailing if isinstance(trailing, ft.Control)
                    else cluster(list(trailing), spacing=S1))

    resting = _alpha(0.09, ft.Colors.PRIMARY) if selected else None

    def on_hover(event):
        event.control.bgcolor = HOVER if _hovered(event) else resting
        try:
            event.control.update()
        except Exception:
            pass

    return ft.Container(
        content=ft.Row(line, spacing=S3,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=_pad(ROW_PAD_H, ROW_PAD_V, ROW_PAD_H, ROW_PAD_V),
        border_radius=_radius(R_MD),
        bgcolor=resting,
        on_click=on_click,
        on_hover=on_hover if on_click is not None else None,
        ink=on_click is not None,
        animate=ANIM_FAST,
    )


def empty(icon, title: str, body: str | None = None, action=None):
    """The empty state: a mark, one sentence, one thing to do.

    Centred and unboxed. An empty list is not an error, so it gets a muted
    glyph rather than a coloured illustration, and it never gets a primary
    button unless there is a real first action to offer.
    """
    parts = [
        ft.Container(
            content=ft.Icon(icon, size=24, color=ft.Colors.OUTLINE),
            width=52, height=52,
            alignment=ft.Alignment.CENTER,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border_radius=R_FULL,
        ),
        text(title, role=ROLE_TITLE, align=ft.TextAlign.CENTER),
    ]
    if body is not None:
        parts.append(text(body, role=ROLE_META, align=ft.TextAlign.CENTER))
    if action is not None:
        parts.append(ft.Container(content=action, margin=ft.Margin(0, S1, 0, 0)))
    return ft.Column(
        parts,
        spacing=S3,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )


def notice(value, *, kind: str = "info", icon=None, action=None):
    """An inline message above the thing it is about: a failed login, an
    unreadable link, a settings file that could not be written.

    A wash of the tone rather than a solid fill, so it reads as a message
    attached to the page and not as a banner sliding in. `value` may be a
    control when the message needs its own layout.
    """
    tones = {
        "info": ft.Colors.PRIMARY,
        "error": ft.Colors.ERROR,
        "success": ft.Colors.TERTIARY,
    }
    if kind not in tones:
        raise ValueError(f"unknown notice kind {kind!r}")
    tint = tones[kind]
    glyph = icon if icon is not None else {
        "info": ft.Icons.INFO_OUTLINE,
        "error": ft.Icons.ERROR_OUTLINE,
        "success": ft.Icons.CHECK_CIRCLE_OUTLINE,
    }[kind]
    body = (value if isinstance(value, ft.Control)
            else text(value, role=ROLE_BODY, expand=True))
    row = [ft.Icon(glyph, size=15, color=tint), body]
    if action is not None:
        row.append(action)
    return ft.Container(
        content=ft.Row(row, spacing=S2 + 1,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=_alpha(0.10, tint),
        border=ft.Border.all(1, _alpha(0.35, tint)),
        border_radius=_radius(R_MD),
        padding=_pad(S3, S2 + 2, S3, S2 + 2),
    )


def danger_zone(body, *, title: str = "Danger zone", pad: int = S4):
    """The bottom of Settings: the things that cannot be undone.

    An error-washed panel with a hairline, deliberately the only tinted
    surface in the app, so "this is different and it is permanent" is legible
    before the words are read.
    """
    return ft.Container(
        content=ft.Column(
            [text(title, role=ROLE_LABEL, color=ft.Colors.ERROR),
             body if isinstance(body, ft.Control) else stack(list(body),
                                                              spacing=S2)],
            spacing=S3,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
        bgcolor=_alpha(0.06, ft.Colors.ERROR),
        border=ft.Border.all(1, _alpha(0.28, ft.Colors.ERROR)),
        border_radius=_radius(R_MD),
        padding=_pad(pad, pad, pad, pad),
    )
