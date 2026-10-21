#!/usr/bin/env python3
"""Open every dialog in the GTK app and check that it really opens.

GTK4 removed `Gtk.Dialog.run()` and `Gtk.Widget.show_all()`. Every
dialog in this app used both, so every one of them raised
AttributeError the moment it was opened. The exception was swallowed by
the button that raised it, so the symptom was not a crash: it was a
button that did nothing at all. "Check for updates" did nothing, the
confirmation never came up, the prompt never appeared.

A window that PAINTS does not catch that. The dialogs are built when
they are opened, and nothing in the app ever opened them, so the app
looked fine and every dialog was broken.

This opens each one, waits for it to be mapped, checks it is really on
screen, and closes it. It runs under a virtual display, so it needs no
desktop.

    xvfb-run -a python scripts/check-gtk-dialogs.py
"""
from __future__ import annotations

import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from telegram_downloader import updates  # noqa: E402
from telegram_downloader.gtk import app as gtk_app  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  {extra}"),
          flush=True)
    if not ok:
        FAILS.append(name)


# -- a fake release, so the update flow can be exercised offline ------------

class _Asset:
    name = "telegram-downloader_9.9.9_amd64.deb"
    url = "https://example.invalid/releases/" + name


class _Info:
    latest_version = "9.9.9"
    release_url = "https://example.invalid/releases"
    assets = [_Asset()]

    def is_newer_than(self, current):
        return True

    def asset_for_platform(self, operating_system=None):
        return _Asset()


updates.check_for_update = lambda *a, **k: _Info()


def fake_download(asset, dest_dir, *args, **kwargs):
    progress = kwargs.get("progress")
    if progress is not None:
        progress(512, 1024)
        progress(1024, 1024)
    return os.path.join(str(dest_dir), asset.name)


updates.download_asset = fake_download


# -- the helpers -------------------------------------------------------------

# A plain Gtk.Dialog is NOT an application window, so the app's window
# list does not contain it. Every dialog the app builds is tracked here
# instead, by wrapping the constructor.
CREATED: list = []
_RealDialog = Gtk.Dialog


class _TrackedDialog(_RealDialog):
    """A dialog that remembers itself.

    A plain Gtk.Dialog is NOT an application window, so the app's
    window list does not contain it -- and the dialogs have to be found
    another way. This is a SUBCLASS rather than a wrapper function:
    GTK's own code does `isinstance(x, Gtk.Dialog)`, and a function
    there raises "isinstance() arg 2 must be a type".
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        CREATED.append(self)
        # Did it actually reach the screen? A dialog that was created
        # and never mapped is not a dialog the user could see.
        self.connect("map", lambda _d: (MAPPED.append(self), False)[1])


Gtk.Dialog = _TrackedDialog

MAPPED: list = []
ANSWERED: set = set()
SEEN: list = []


def answer_dialogs(delay_ms: int = 150):
    """Answer whatever dialog appears, so a blocking one can finish.

    Without this the check would hang on the first modal, and a check
    that hangs is a check nobody runs.
    """
    def _try():
        for dialog in CREATED:
            if dialog.get_visible() and dialog not in ANSWERED:
                ANSWERED.add(dialog)
                SEEN.append(dialog)
                dialog.close()
                return False
        return True

    GLib.timeout_add(delay_ms, _try)


def was_seen(dialog) -> bool:
    """Was this dialog on screen?

    "Seen" rather than "mapped": the answer can close a dialog before
    the map event is delivered, and the dialog WAS on screen either
    way.
    """
    return dialog in SEEN or dialog in MAPPED


# -- the app -----------------------------------------------------------------

app = gtk_app.App()
app.register(None)
app.activate()

view = app.views["settings"]


# -- 1. the confirmation -----------------------------------------------------
answer_dialogs()
try:
    answer = app.confirm("Install this update?", "Body text.", ok_label="Install")
    check("the confirmation opens and answers", answer is False,
          f"answer={answer!r}")
except Exception as exc:
    check("the confirmation opens and answers", False, f"{exc!r}")
check("the confirmation was on screen",
      any(was_seen(d) for d in CREATED))

# -- 2. the prompt -----------------------------------------------------------
before = len(CREATED)
answer_dialogs()
try:
    text = app.prompt("Copy this command", "", "", value="echo hi")
    check("the prompt opens and answers", text is None, f"text={text!r}")
except Exception as exc:
    check("the prompt opens and answers", False, f"{exc!r}")
check("the prompt was on screen",
      any(was_seen(d) for d in CREATED[before:]))

# -- 3. the non-blocking prompt ---------------------------------------------
before = len(CREATED)
got = []
app.prompt_async("Copy this command", "", "", callback=got.append, value="x")
answer_dialogs()
for _ in range(200):
    if got:
        break
    GLib.MainContext.default().iteration(True)
check("the non-blocking prompt was on screen",
      any(was_seen(d) for d in CREATED[before:]))
check("the non-blocking prompt answers", got == [None], f"got={got!r}")

# -- 4. the non-blocking confirmation ---------------------------------------
before = len(CREATED)
answers = []
app.confirm_async("Install this update?", "Body text.",
                   callback=answers.append, ok_label="Install")
answer_dialogs()
for _ in range(200):
    if answers:
        break
    GLib.MainContext.default().iteration(True)
check("the non-blocking confirmation was on screen",
      any(was_seen(d) for d in CREATED[before:]))
check("the non-blocking confirmation answers", answers == [False],
      f"answers={answers!r}")

# -- 5. the update dialog ----------------------------------------------------
before = len(CREATED)
answer_dialogs()
try:
    view._update_dialog(_Info())
    check("the update dialog opens and answers", True)
except Exception as exc:
    check("the update dialog opens and answers", False, f"{exc!r}")
check("the update dialog was on screen",
      any(was_seen(d) for d in CREATED[before:]))

# -- 6. the download progress dialog -----------------------------------------
before = len(CREATED)
view._update_progress.update(done=0, total=0, finished=False)
try:
    view._progress_open()
    check("the progress dialog opens", view._progress_dialog is not None)
    view._update_progress.update(done=512, total=1024, finished=False)
    view._progress_poll()
    fraction = view._progress_bar.get_fraction()
    check("the progress bar moves", abs(fraction - 0.5) < 0.01, f"{fraction}")
    view._update_progress.update(done=1024, total=1024, finished=True)
    view._progress_poll()
    check("the progress dialog closes", view._progress_dialog is None)
except Exception as exc:
    check("the progress dialog opens", False, f"{exc!r}")
check("the progress dialog was on screen",
      any(was_seen(d) for d in CREATED[before:]))

# -- 7. the whole "press the button" path ------------------------------------
# The dialog opening is not the same as the BUTTON working. The check
# runs the real handler -- the one the "Check for updates" button is
# wired to -- and waits for the dialog it is supposed to produce.
before = len(CREATED)
answer_dialogs()
try:
    asyncio.run(view._on_check_updates())
except Exception as exc:
    check("the update check reaches a dialog", False, f"{exc!r}")
# The handler runs on the engine's loop and hands the dialog to the GTK
# thread, so the main context has to be given a chance to run it.
for _ in range(200):
    if any(was_seen(d) for d in CREATED[before:]):
        break
    GLib.MainContext.default().iteration(True)
check("the update check reaches a dialog",
      any(was_seen(d) for d in CREATED[before:]))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("OK: every dialog in the GTK app opens, answers and closes")
