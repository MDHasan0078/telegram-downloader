"""The design tokens exist once, and the Android theme is generated from them.

The palette, the spacing scale, the radii and the type scale live in
`src/telegram_downloader/gtk/tokens.py`. The Android app needs the same
numbers in Kotlin, so `scripts/gen_android_theme.py` generates the
numeric blocks of `Theme.kt` from that file.

Two things then have to hold, and both are checked here:

  1. The checked-in `Theme.kt` is what the generator produces. If a
     palette value is changed in `tokens.py` and the Kotlin is not
     regenerated, the build fails instead of shipping two different
     colours for the same token.

  2. Every token in the Python palette has a field in the Kotlin
     `TgColors` class. The generator emits all of them, so a token
     added to the Python side without a matching Kotlin field is a
     compile error waiting to happen -- and it is invisible until
     someone builds the APK, which is a twelve minute round trip.

The second check is not hypothetical: `shadow` and `scrim` were in the
Python palette and not in `TgColors`, so generating the palette from
Python produced Kotlin that did not compile.
"""
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import gen_android_theme  # noqa: E402
from telegram_downloader.gtk import tokens  # noqa: E402

THEME = gen_android_theme.THEME


def test_theme_is_generated_from_the_python_tokens():
    """The checked-in Kotlin is exactly what the generator emits."""
    assert gen_android_theme.render() == THEME.read_text(encoding="utf-8"), (
        "Theme.kt is stale against gtk/tokens.py -- run "
        "scripts/gen_android_theme.py --write"
    )


def test_generator_check_passes():
    """The CLI entry point CI runs agrees with the file on disk."""
    assert gen_android_theme.main(["--check"]) == 0


def test_generator_is_idempotent():
    """Rendering twice gives the same text, so --write is safe to re-run."""
    once = gen_android_theme.render()
    assert gen_android_theme.render() == once


@pytest.mark.parametrize("mode", ["DARK", "LIGHT"])
def test_every_python_token_has_a_kotlin_field(mode):
    """A token in the Python palette must exist in the Kotlin class.

    The generator emits the whole palette, so a missing Kotlin field is
    a compile error ("cannot find parameter"), not a silent omission.
    """
    text = THEME.read_text(encoding="utf-8")
    block = re.search(r"data class TgColors\((.*?)\n\)", text, re.S)
    assert block, "TgColors class not found in Theme.kt"
    fields = set(re.findall(r"val (\w+): Color", block.group(1)))

    palette = getattr(tokens, mode)
    expected = {gen_android_theme._camel(key) for key in palette}
    missing = expected - fields
    assert not missing, (
        f"{mode} palette tokens with no field in TgColors: {sorted(missing)}"
    )


def test_kotlin_palette_values_match_python():
    """Every generated colour literal equals the Python value.

    This is a belt-and-braces check on the generator itself: it reads
    the value out of the generated file and compares it to the source,
    so a bug in the formatting (a missing '#', a wrong alpha form)
    cannot slip through as a valid-looking but wrong colour.
    """
    text = THEME.read_text(encoding="utf-8")
    for mode, name in (("DARK", "Dark"), ("LIGHT", "Light")):
        block = re.search(
            rf"val {name} = TgColors\((.*?)\n    \)", text, re.S)
        assert block, f"{name} palette not found in Theme.kt"
        for key, value in getattr(tokens, mode).items():
            field = gen_android_theme._camel(key)
            assert re.search(
                rf"{field} = {re.escape(gen_android_theme._colour(value))},",
                block.group(1)), (
                f"{mode}.{key} ({value}) is not in the {name} palette"
            )


def test_generated_type_scale_uses_valid_kotlin_sp():
    """`10.5sp` is not Kotlin; it must be `10.5.sp`."""
    text = THEME.read_text(encoding="utf-8")
    assert not re.search(r"\dsp\b", text), "a size is missing its dot before 'sp'"
    for size in ("10.5.sp", "11.5.sp", "12.5.sp", "13.5.sp", "14.5.sp", "22.sp"):
        assert size in text, f"{size} is missing from the type scale"


def test_generated_blocks_are_actually_present():
    """Each generated region survives, and nothing outside it was eaten.

    A greedy regex once swallowed the tail of the file -- the theme
    composable and the CompositionLocal went with it -- and the file
    still looked plausible. The markers are the contract.
    """
    text = THEME.read_text(encoding="utf-8")
    for name in ("palette", "space", "radius", "type"):
        assert f"// BEGIN GENERATED {name}" in text, f"no {name} block"
        assert f"// END GENERATED {name}" in text, f"no {name} block end"
    # The hand-written parts after the last generated block must survive.
    assert "LocalTgColors" in text, "the palette local was lost"
    assert "fun TgTheme(" in text, "the theme composable was lost"
    assert "fun tgColors()" in text, "the palette accessor was lost"
