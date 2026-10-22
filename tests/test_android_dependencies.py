"""What the app tells a phone user about its dependencies.

The complaint this answers: on a phone the Settings screen listed
ffmpeg and cryptg as MISSING, in red, with a line saying there was no
way to install them. A red MISSING badge is a claim -- "this is
missing, and here is what to do about it" -- and on a phone there is
no shell, no compiler and no package manager, so the user could do
nothing about it. The app was asking a person to fix something the
app did not even need: it does not offer the MP4 modes on Android, and
Telethon's pure-Python crypto works.

So on Android the rows that do not apply are left out of the list, and
the list says nothing about them.
"""
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from telegram_downloader import deps as deps_mod  # noqa: E402


def _as_android(monkeypatch):
    """Make the dependency checker think it is running on a phone."""
    monkeypatch.setattr(deps_mod, "_on_android", lambda: True)


def test_on_a_desktop_every_dependency_is_listed():
    """The desktop list is untouched: everything there CAN be installed."""
    rows = deps_mod.check_all()
    names = {d.name for d in rows}
    assert any("ffmpeg" in n for n in names)
    assert any("cryptg" in n for n in names)
    assert all(d.applicable for d in rows), [
        d.name for d in rows if not d.applicable]


def test_on_a_phone_the_rows_that_cannot_apply_are_left_out(monkeypatch):
    _as_android(monkeypatch)
    rows = {d.name: d for d in deps_mod.check_all()}

    ffmpeg = next(d for n, d in rows.items() if "ffmpeg" in n)
    cryptg = next(d for n, d in rows.items() if "cryptg" in n)

    # Not applicable: the app cannot use them and the user cannot
    # install them.
    assert not ffmpeg.applicable
    assert not cryptg.applicable

    # The ones that are there are all satisfied -- a phone shows no red
    # dots at all.
    shown = [d for d in rows.values() if d.applicable]
    assert shown, "the list would be empty"
    assert all(d.ok for d in shown), [
        (d.name, d.hint) for d in shown if not d.ok]

    # And the truth about them is still told, in the hint, for anyone
    # who reads the source or the CLI.
    assert "Android" in ffmpeg.hint
    assert "Android" in cryptg.hint


def test_the_required_dependencies_are_still_required(monkeypatch):
    """Hiding a row must never hide a REQUIRED one.

    `missing_required()` is the launch check: it stops the app when
    telethon is missing. A row that does not apply is never required,
    so this cannot be quietly broken by the rule above.
    """
    _as_android(monkeypatch)
    rows = {d.name: d for d in deps_mod.check_all()}
    telethon = next(d for n, d in rows.items() if "telethon" in n)
    assert telethon.required
    assert telethon.applicable
    assert telethon.ok


def test_android_is_detected_without_the_cpypyon_api(monkeypatch):
    """CPython 3.12 has no getandroidapilevel, and the Flet build does
    not use Chaquopy.

    The Android runtime sets ANDROID_PRIVATE in every app, which is how
    the Flet build is told apart from a desktop Linux -- and
    `platform.system()` says "Linux" on both.

    ANDROID_ROOT is NOT used: that is the legacy name for the Android
    SDK's directory, so it is set on a build machine too, and the CI
    runner would report itself as a phone.
    """
    from telegram_downloader import _android_paths

    monkeypatch.delattr("sys.getandroidapilevel", raising=False)
    monkeypatch.setenv("ANDROID_PRIVATE", "/data/user/0/com.example")
    assert _android_paths.is_android() is True

    monkeypatch.delenv("ANDROID_PRIVATE", raising=False)
    monkeypatch.setenv("ANDROID_ROOT", "/opt/android-sdk")
    assert _android_paths.is_android() is False, \
        "ANDROID_ROOT is the SDK's directory, not a phone's"
