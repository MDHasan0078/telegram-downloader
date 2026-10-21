#!/usr/bin/env python3
"""Generate the Android theme's tokens from the GTK ones.

The design system exists once, in `src/telegram_downloader/gtk/tokens.py`.
The Android app needs the same numbers, in Kotlin. Keeping a second
hand-maintained copy of a palette is how the two GUIs drift -- and a
drifted palette is invisible until someone puts them side by side and
notices the Android app is a slightly different colour.

So Theme.kt's numeric tokens are GENERATED from tokens.py, and
`--check` fails if the checked-in file no longer matches. The generator
is the only thing that writes the block; a human editing it by hand
gets a failing check.

    scripts/gen_android_theme.py --check    # exit 1 if stale (used by CI)
    scripts/gen_android_theme.py --write    # rewrite the generated blocks
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from telegram_downloader.gtk import tokens  # noqa: E402

THEME = REPO / ("android/app/src/main/kotlin/com/tgdownloader/"
                 "telegram_downloader/Theme.kt")

BEGIN = "    // BEGIN GENERATED"
END = "    // END GENERATED"


def _camel(name: str) -> str:
    """well_deep -> wellDeep, on_accent_wash -> onAccentWash."""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _colour(value: str) -> str:
    """'#0F1318' -> 'Color(0xFF0F1318)'; '#FFFFFF,0.09' -> alpha form."""
    if "," in value:
        hex_part, alpha = value.split(",", 1)
        return f"Color(0xFF{hex_part.lstrip('#').upper()}).copy(alpha = {alpha}f)"
    return f"Color(0xFF{value.lstrip('#').upper()})"


def _palette_block(name: str, palette: dict) -> str:
    lines = [f"    val {name} = TgColors("]
    for key, value in palette.items():
        lines.append(f"        {_camel(key)} = {_colour(value)},")
    lines.append("    )")
    return "\n".join(lines)


def _space_block() -> str:
    lines = ["    val s1: Dp = %d.dp" % tokens.S1]
    for i in range(2, 9):
        lines.append(f"    val s{i}: Dp = {getattr(tokens, f'S{i}')}.dp")
    lines.append("")
    lines.append(f"    val pagePad: Dp = {tokens.PAGE_PAD}.dp")
    lines.append(f"    val barHeight: Dp = {tokens.BAR_H}.dp")
    lines.append(f"    val rowPadH: Dp = {tokens.ROW_PAD_H}.dp")
    lines.append(f"    val rowPadV: Dp = {tokens.ROW_PAD_V}.dp")
    return "\n".join(lines)


def _radius_block() -> str:
    return "\n".join([
        f"    val sm: Dp = {tokens.R_SM}.dp",
        f"    val md: Dp = {tokens.R_MD}.dp",
        f"    val lg: Dp = {tokens.R_LG}.dp",
        f"    val full: Dp = {tokens.R_FULL}.dp",
    ])


def _type_block() -> str:
    comments = {
        "micro": "badges, chips",
        "meta": "status lines, captions, field hints",
        "label": "section labels, field labels",
        "body": "base: body copy, buttons, nav, inputs, values",
        "title": "row titles, the wordmark, dialog titles",
        "display": "empty-state headline",
    }
    lines = []
    for py, kt in (("T_MICRO", "micro"), ("T_META", "meta"), ("T_LABEL", "label"),
                   ("T_BODY", "body"), ("T_TITLE", "title"),
                   ("T_DISPLAY", "display")):
        value = getattr(tokens, py)
        # Kotlin wants `10.5.sp`, not `10.5sp`.
        text = f"{value}.sp" if value % 1 else f"{int(value)}.sp"
        lines.append(f"    val {kt} = {text}".ljust(23)
                     + f" // {comments[kt]}")
    return "\n".join(lines)


BLOCKS = {
    "palette": "\n".join([
        _palette_block("Dark", tokens.DARK),
        "",
        _palette_block("Light", tokens.LIGHT),
    ]),
    "space": _space_block(),
    "radius": _radius_block(),
    "type": _type_block(),
}


def render() -> str:
    """The whole file, with the generated blocks substituted in."""
    text = THEME.read_text(encoding="utf-8")
    for name, body in BLOCKS.items():
        pattern = re.compile(
            rf"{re.escape(BEGIN)} {name}\n.*?\n{re.escape(END)} {name}",
            re.S)
        replacement = f"{BEGIN} {name}\n{body}\n{END} {name}"
        text, count = pattern.subn(lambda _m: replacement, text, count=1)
        if count != 1:
            raise SystemExit(
                f"gen_android_theme: no '{name}' block found in {THEME}. "
                f"Expected a region between '{BEGIN} {name}' and "
                f"'{END} {name}'.")
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true",
                       help="fail if the checked-in file is stale")
    group.add_argument("--write", action="store_true",
                       help="rewrite the generated blocks")
    args = ap.parse_args(argv)

    rendered = render()
    if args.write:
        THEME.write_text(rendered, encoding="utf-8")
        print(f"  wrote {THEME.name}")
        return 0

    current = THEME.read_text(encoding="utf-8")
    if current == rendered:
        print(f"  {THEME.name} is up to date with gtk/tokens.py")
        return 0
    print("::error::Theme.kt is stale against gtk/tokens.py.", file=sys.stderr)
    print("  Run: scripts/gen_android_theme.py --write", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
