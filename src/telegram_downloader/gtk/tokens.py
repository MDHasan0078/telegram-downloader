"""The Telegram Downloader design tokens, as Python.

A port of `telegram_downloader.ui`'s palette, spacing and type scale to plain
Python values, so the GTK stylesheet is BUILT from the same numbers the Flet
app used instead of a second, drifting copy.

The palette is not decorative here. Three rules ride on it:

1. ONE ACCENT, AND IT MEANS ACTION. `accent` marks a thing you can DO --
   the primary button, a progress bar, a focus ring. It never marks where you
   are: the selected destination is a RAISED surface with a hairline, the same
   material as the selected segment of a segmented control.
2. MATERIALS, NOT BOXES. `page` is the window, `well`/`well_deep` are BELOW it
   (input fills, troughs, the nav strip) and `raised`/`floating` are ABOVE it
   (the selected pill, the bottom bar, dialogs). Every surface is a tonal
   step, never a shadowed card.
3. THE MEANING INVERTS CLEANLY. "Well" is always lower than the page and
   "raised" is always higher, in both themes, so a role can render bright in
   one theme and dark in the other without the ramp flipping direction.

Contrast ratios are documented per token in ui.py and are already checked:
ink 14.5:1 on the page, muted 8.3:1, accent 9.0:1, outline 3.44:1. Keep them.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Palette
#
# Values are OKLCH conversions, not hand-picked hex, and each ramp is monotonic
# in lightness so "one step up" is one number. The ladders:
#
#   dark   well_deep .185  well .220  page .248  raised .295  floating .340
#   light  well_deep .928  well .952  page .978  raised 1.000 floating 1.000
#
# Light mode needs no floating step above white, so raised and floating are
# the same value there and depth comes from the hairline instead.
#
# Alpha hairlines ("#FFFFFF,0.09") are Flet's own with_opacity output format.
# GTK needs rgba(), so `theme.py` converts; the values here are the source of
# truth for both.
# ---------------------------------------------------------------------------

DARK = {
    # -- surfaces, deepest first
    "well_deep": "#0F1318",   # input fill, progress trough
    "well": "#161B20",        # nav strip, segmented-control trough
    "page": "#1D2227",        # the window itself
    "raised": "#282D32",      # selected nav pill, hovered row, bottom bar
    "floating": "#34383E",    # dialogs, toasts
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
    "edge": "#000000,0.06",       # a soft top edge
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
    "success_wash": "#D3F1F9",
    "on_success_wash": "#0A562B",
    "error": "#BE222A",           # 5.7:1
    "on_error": "#FFFFFF",
    "error_wash": "#FFE0DC",
    "on_error_wash": "#90101A",
    "shadow": "#000000",
    "scrim": "#000000,0.35",
}

# ---------------------------------------------------------------------------
# Space, radius, type
#
# Spacing is 4-based with an 8 anchor, used in two tiers: 4/8/12 inside a
# component (icon to label, row padding), 16/24/32 between blocks. The 4px
# half step exists because a desktop row needs a gap tighter than 8 or the
# icon floats away from its label.
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

# Three radii with visible separation (6 / 10 / 16), scaled to the object
# they are on: a control inside a row, a panel or button, then a floating
# sheet. The size step between them is ~1.6x, so "rounder" reads as a level.
R_SM = 6            # chips, icon buttons, segments
R_MD = 10           # buttons, inputs, rows with a fill, menus
R_LG = 16           # sheets, dialogs, and the logo tile
R_FULL = 999        # pills, dots, progress bars

# Six sizes, 10.5 to 14.5 in 1px steps plus one jump. The steps are
# deliberately small: the queue is a long list, and a 2px jump between a row
# title and its status line shouts where the eye only needs a nudge.
T_MICRO = 10.5      # badges, chips
T_META = 11.5       # status lines, captions, field hints
T_LABEL = 12.5      # section labels, field labels
T_BODY = 13.5       # base: body copy, buttons, nav, inputs, values
T_TITLE = 14.5      # row titles, the wordmark, dialog titles
T_DISPLAY = 22      # empty-state headline: the only thing on screen

# The app cannot ship a font (the package has no assets_dir and the .deb must
# stay dependency-light), so it asks for the desktop's own UI face and lists
# the rest of the family in order.
FONT_UI = "Cantarell"
FONT_UI_STACK = ["Ubuntu", "Noto Sans", "DejaVu Sans", "Liberation Sans",
                 "sans-serif"]
FONT_MONO = "DejaVu Sans Mono"
FONT_MONO_STACK = ["Ubuntu Mono", "Liberation Mono", "Noto Sans Mono",
                   "Consolas", "monospace"]


def palette(dark: bool = True) -> dict:
    """The token dict for one mode. Dark is the one the app ships in."""
    return DARK if dark else LIGHT
