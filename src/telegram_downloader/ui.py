"""Design system for the Flet GUI.

Ported from simple-yt-downloader's `style.py`, which is native GTK rather than
Flet -- so this is the design *language*, not its code. The reference states the
rule this module exists to enforce:

    One card language for video rows, playlist rows, loading rows, and the
    download-path bar.

Cards are for LIST ITEMS and the path bar. Everything else -- section headers,
form fields, button rows -- sits directly on the scaffold with a muted label.
Wrapping every section in its own bordered box (what this GUI used to do)
fragments the page and leaves nothing visually primary.

Surfaces are navy-tinted neutrals; the accent is Telegram brand blue. The accent
is only ever a small mark -- a 3px tab underline, a 28px icon button, one
primary button. Earlier revisions used #2AABEE as a full-bleed background, which
turned the one accent colour into a slab across the whole window.
"""
from __future__ import annotations

import flet as ft

# -- Tokens ------------------------------------------------------------------
# Navy cannot serve as the accent on a dark theme: #1B2A4A on #0B101C is about
# 1.2:1. Navy is the surface family; blue is the accent. Each does the job it is
# actually good at.

DARK = dict(
    bg="#0B101C",            # scaffold, navy-black
    surface="#121A2B",       # card / path bar
    surface_alt="#182236",   # one step in: inputs, inset fills
    border="#24324D",        # hairline, ~= alpha(fg, .16) over bg
    border_strong="#334566",
    text="#E4EAF5",
    muted="#93A2BD",
    faint="#64748F",
    accent="#2AABEE",        # Telegram brand blue
    accent_hover="#3FB6F3",
    accent_fg="#071320",     # ink on accent; 7.3:1, same as white on #2AABEE
    accent_soft="#0E2B40",   # selected tab / active toggle wash
    success="#2DA44E",
    error="#E5484D",
    warning="#B45309",
)

LIGHT = dict(
    bg="#EEF0F6",
    surface="#FFFFFF",
    surface_alt="#F4F6FB",
    border="#D3D8E6",
    border_strong="#AEB6CC",
    text="#141A26",
    muted="#59637A",
    faint="#8A93A8",
    accent="#1E88C7",        # darkened for contrast on light
    accent_hover="#1877B0",
    accent_fg="#FFFFFF",
    accent_soft="#DCEBF6",
    success="#1F7A3D",
    error="#C62A2F",
    warning="#9A5B06",
)

# Type scale. Four steps, mirroring the reference's 11/12/13/600 mix. The old
# GUI carried sixteen sizes from 10.5 to 56, which is drift rather than a scale.
T_TITLE = 19      # page title
T_HEAD = 13       # section heading, card title
T_BODY = 12.5     # body copy
T_LABEL = 11.5    # field labels, muted
T_META = 11       # captions, status lines
T_BADGE = 10.5    # chips

RADIUS = 12       # card radius, from .download-row
GAP = 12          # card padding, from .download-row
GUTTER = 16       # window margin, from settings_view.set_margin_start


def theme(dark: bool = True) -> ft.Theme:
    """Build a Flet theme from a token set."""
    t = DARK if dark else LIGHT
    th = ft.Theme(
        color_scheme_seed="#2AABEE",
        scaffold_bgcolor=t["bg"],
        color_scheme=ft.ColorScheme(
            primary=t["accent"],
            on_primary=t["accent_fg"],
            primary_container=t["accent_soft"],
            on_primary_container=t["accent"],
            secondary=t["muted"],
            on_secondary=t["accent_fg"],
            secondary_container=t["surface_alt"],
            on_secondary_container=t["text"],
            tertiary=t["success"],
            on_tertiary=t["accent_fg"],
            tertiary_container="#12301F",
            on_tertiary_container="#7FD8A0",
            error=t["error"],
            on_error=t["accent_fg"],
            error_container="#3A1216",
            on_error_container="#FFB4AB",
            surface=t["surface"],
            on_surface=t["text"],
            surface_container_high=t["surface_alt"],
            on_surface_variant=t["muted"],
            outline=t["border"],
        ),
    )
    # Hidden at rest, shown while scrolling. The GTK reference does the same
    # (scrollbar { background: none }) and a permanent groove was the loudest
    # part of Flutter's default.
    th.scrollbar_theme = ft.ScrollbarTheme(
        thumb_visibility=False,
        track_visibility=False,
        thickness=3,
        radius=1.5,
        min_thumb_length=48,
        interactive=True,
    )
    return th


# -- Primitives --------------------------------------------------------------

def card(body, *, pad: int = GAP, radius: int = RADIUS,
         bgcolor: str | None = None, border: str | None = None):
    """The single card language: surface, 1px hairline, 12px radius, 12px pad.

    Used for list items, the path bar, and the two Settings cards that earn one
    (About, Dependencies). NOT for section headers or form groups.
    """
    if isinstance(body, list):
        body = ft.Column(body, spacing=8, tight=True)
    return ft.Container(
        body,
        bgcolor=bgcolor or ft.Colors.SURFACE,
        border=ft.Border.all(1, border or ft.Colors.OUTLINE_VARIANT),
        border_radius=radius,
        padding=pad,
    )


def section_label(text: str) -> ft.Text:
    """A muted section header with no box around it.

    This replaces the old `section()` cards. The reference builds its whole
    Settings page from these, and the page reads as one continuous form rather
    than a stack of separate panels.
    """
    return ft.Text(text, size=T_LABEL, weight=ft.FontWeight.W_600,
                   color=ft.Colors.ON_SURFACE_VARIANT)


def caption(text: str) -> ft.Text:
    """Faint 11px secondary line."""
    return ft.Text(text, size=T_META, color=ft.Colors.ON_SURFACE_VARIANT)


def mono(text: str, size: int = T_META, color: str | None = None):
    """Machine-level value in a monospace face: paths, versions, commands.

    Mirrors the reference's rule that paths/ports/versions read differently from
    prose. 'monospace' is a generic family Flutter resolves on every desktop
    target, so nothing has to be downloaded at runtime.
    """
    return ft.Text(text, size=size, font_family="monospace",
                   color=color or ft.Colors.ON_SURFACE_VARIANT)


def count_badge(text: str) -> ft.Container:
    """11px chip, ~= alpha(fg, .09) on surface, 8px radius."""
    return ft.Container(
        ft.Text(text, size=T_BADGE, weight=ft.FontWeight.W_600,
                color=ft.Colors.ON_SURFACE_VARIANT),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
        border_radius=8,
        padding=ft.Padding(7, 2, 7, 2),
    )


def icon_button(icon, tooltip: str, on_click=None, *, destructive: bool = False,
                size: int = 16, disabled: bool = False):
    """Flat 28x28 icon-only button, from button.image-button.

    Used for the row actions (cancel / pause / retry) and the header cluster.
    The old GUI used text buttons here, which is what made the queue rows noisy.
    Destructive icons get the error red because muted text washes out on dark.
    """
    color = ft.Colors.ERROR if destructive else ft.Colors.ON_SURFACE_VARIANT
    return ft.Container(
        ft.Icon(icon, size=size, color=color),
        width=28, height=28,
        alignment=ft.Alignment.CENTER,
        border_radius=7,
        opacity=0.4 if disabled else 1,
        tooltip=tooltip,
        on_click=None if disabled else on_click,
        ink=True,
    )


def linked_toggle(options, selected, on_change):
    """Joined toggle buttons: checked segment is filled with the accent.

    Mirrors `box.linked > button:checked { background-color: @accent }`, used by
    the reference for Video|Audio and for Light|Dark. Built from primitives
    because it needs two visual states -- filled and flat -- driven by data,
    which ft.SegmentedButton does not expose cleanly on 0.86.5.
    """
    state = {"sel": selected}

    def build():
        row = ft.Row(spacing=0, tight=True)
        for value, label in options:
            on = state["sel"] == value
            seg = ft.Container(
                # ON_PRIMARY on the checked segment, NOT primary: the label sat
                # in accent-on-accent and vanished, leaving a solid blue block
                # with no readable text in it.
                content=ft.Text(label, size=T_BODY,
                                weight=(ft.FontWeight.W_600 if on
                                        else ft.FontWeight.W_500),
                                color=(ft.Colors.ON_PRIMARY if on
                                       else ft.Colors.ON_SURFACE_VARIANT)),
                bgcolor=(ft.Colors.PRIMARY if on
                         else ft.Colors.TRANSPARENT),
                padding=ft.Padding(14, 7, 14, 7),
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=7,
                data=value,
                on_click=lambda e, v=value: _pick(v),
            )
            row.controls.append(seg)
        return row

    def _pick(value):
        state["sel"] = value
        ctl = holder.content
        ctl.controls = build().controls
        try:
            ctl.update()
        except Exception:
            pass
        on_change(value)

    holder = ft.Container(content=build())
    return holder


def progress_bar(value: float, *, color=None, height: int = 6):
    """6px bar, 3px radius, sunk into a faint trough.

    The reference moved the numbers out of the bar and into the status line
    beneath it, which is why its bar is this thin.
    """
    return ft.ProgressBar(
        value=max(0.0, min(1.0, value)),
        bar_height=height,
        border_radius=3,
        color=color or ft.Colors.PRIMARY,
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
    )


def primary_button(label: str, icon, on_click=None, *, disabled: bool = False):
    """Accent-filled action, from button.suggested-action: 30px tall, 8px radius."""
    return ft.Container(
        content=ft.Row(
            [ft.Icon(icon, size=15, color=ft.Colors.ON_PRIMARY),
             ft.Text(label, size=T_BODY, weight=ft.FontWeight.W_600,
                     color=ft.Colors.ON_PRIMARY)],
            spacing=7, tight=True,
        ),
        bgcolor=ft.Colors.PRIMARY if not disabled else ft.Colors.SURFACE_CONTAINER_HIGH,
        border_radius=8,
        padding=ft.Padding(16, 8, 16, 8),
        opacity=0.55 if disabled else 1,
        on_click=None if disabled else on_click,
        ink=True,
    )


def flat_button(label: str, icon, on_click=None, *, destructive: bool = False,
                width: int | None = None):
    """Secondary action: no fill, hairline border, ink-coloured text."""
    color = ft.Colors.ERROR if destructive else ft.Colors.ON_SURFACE
    body = ft.Row(
        ([ft.Icon(icon, size=15, color=color)] if icon is not None else [])
        + [ft.Text(label, size=T_BODY, color=color)],
        spacing=7, tight=True,
    )
    ctl = ft.Container(
        content=body,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        padding=ft.Padding(14, 8, 14, 8),
        alignment=ft.Alignment.CENTER,
        on_click=on_click,
        ink=True,
    )
    if width:
        ctl.width = width
    return ctl


def field(control, *, hint: str | None = None):
    """Wrap a control with a caption above and an optional right-aligned hint.

    Flet otherwise sizes a TextField to its content, so fields stop mid-card;
    STRETCH makes them fill whatever they are given, which is what the reference
    does by letting GTK entries span the window.
    """
    head = ft.Text(hint or "", size=T_META, font_family="monospace",
                   color=ft.Colors.ON_SURFACE_VARIANT)
    return ft.Column(
        [head, control], spacing=4,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
