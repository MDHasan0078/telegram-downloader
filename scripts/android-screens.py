#!/usr/bin/env python3
"""Visit every screen of the native app, and look at each one.

The launch check only ever saw ONE screen: the one the app opens on. A
top bar that is missing its wordmark is invisible in that case -- the
bar is on every screen, so it is wrong on all of them, and the check
looked at the one screen whose content was right.

This walks the destinations, taps each one BY ITS OWN LABEL (so it does
not depend on a pixel position that changes with the layout), and writes
a screenshot and the accessibility tree for each. Every screen is
looked at, and every screen is asserted to show its OWN strings: a
destination that shows the previous screen's content is a broken app
that no screenshot of one screen would ever reveal.

The bar is WIDER than a phone, so its destinations scroll, and a
destination that is scrolled off the bar is not in the accessibility
tree at all. So a label that is not there is not a failure: the bar is
scrolled and the label is looked for again.

    python scripts/android-screens.py --out dist
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import time

# The destinations, and the strings that prove the right screen is up.
#
# "Add" and "Ongoing" and "Account auth" and "Settings" are the chip
# labels -- the same text on every screen, so they are the TABS and not
# the content. The content is what differs, so each screen lists
# strings that appear on IT and on no other.
#
# A screen passes if ANY of its strings is there: some of them are
# conditional (the queue's header only exists when the queue is not
# empty), so demanding all of them would be demanding a state.
#
# The last list is the other way round: strings that must NOT be on the
# screen. The Settings screen lists the dependencies, and flet is not one
# of them -- this app is Kotlin/Compose and never imports flet, so a red
# MISSING badge for it would be a false alarm about something the app
# cannot use.
SCREENS = [
    ("add", "Add", ["Telegram link(s)", "Load .txt"], []),
    ("ongoing", "Ongoing", ["Downloads (", "Clear finished",
                            "No downloads in queue"], []),
    ("auth", "Account auth", ["Telegram account", "API ID", "Log in"], []),
    ("settings", "Settings", ["Dependencies", "Check for updates"], ["flet"]),
]


def adb(*args: str, timeout: int = 120) -> str:
    proc = subprocess.run(["adb", *args], capture_output=True, text=True,
                          timeout=timeout)
    return proc.stdout or ""


def dump_tree() -> str:
    """The accessibility tree, as XML on one line."""
    adb("shell", "uiautomator", "dump", "/sdcard/tg-ui.xml")
    return adb("shell", "cat", "/sdcard/tg-ui.xml")


def node_bounds(xml: str, text: str):
    """The centre of the node whose text is exactly `text`."""
    for match in re.finditer(r"<node[^>]*>", xml):
        node = match.group(0)
        found = re.search(r'text="([^"]*)"', node)
        if not found or found.group(1) != text:
            continue
        box = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if not box:
            continue
        x1, y1, x2, y2 = (int(g) for g in box.groups())
        return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def screen_size():
    """The screen's size in pixels, or None if it cannot be read."""
    try:
        out = adb("shell", "wm", "size")
    except OSError:
        return None
    found = re.search(r"(\d+)x(\d+)", out)
    if not found:
        return None
    return int(found.group(1)), int(found.group(2))


def strip_y(tree: str):
    """The y of the bar's chip strip: the y of any chip label.

    The strip is at the top of the screen and the chip labels are the
    only thing in it, so a label's y IS the strip's y.
    """
    for label in ("Add", "Ongoing", "Account auth", "Settings"):
        spot = node_bounds(tree, label)
        if spot:
            return spot[1]
    return None


def swipe_strip(tree: str, towards_end: bool = True) -> None:
    """Scroll the bar's chip strip.

    The bar is wider than a phone, so the destinations scroll, and one
    of them can be off the strip at any moment. A destination that is
    off the strip is not in the accessibility tree -- Compose clips a
    node that is scrolled out of its scrollable, and a node with no
    bounds left is not reported at all -- so the strip is scrolled and
    the label is looked for again.

    `towards_end` scrolls towards the LAST destination; the other way
    scrolls back to the first. The swipe is across the bar, not from an
    edge: the back gesture lives on the edges.
    """
    y = strip_y(tree)
    size = screen_size()
    if y is None or size is None:
        return
    width = size[0]
    if towards_end:
        start, end = int(width * 0.8), int(width * 0.2)
    else:
        start, end = int(width * 0.2), int(width * 0.8)
    adb("shell", "input", "swipe", str(start), str(y), str(end), str(y), "300")
    time.sleep(1.0)


def label_is_at_the_end(tree: str, label: str) -> bool:
    """Which way the strip has to move for `label` to be properly on it.

    A label in the right half of the screen is at the END of the strip
    and needs it scrolled that way; one in the left half is at the
    beginning of it.
    """
    spot = node_bounds(tree, label)
    size = screen_size()
    if spot is None or size is None:
        return True
    return spot[0] > size[0] / 2


def texts_in(xml: str) -> set[str]:
    return {t for t in re.findall(r'text="([^"]*)"', xml) if t.strip()}


def boxes_of(xml: str, needle: str):
    """Every node whose text contains `needle`, as a box."""
    return [box for box, text in _nodes(xml) if needle.lower() in text.lower()]


def _nodes(xml: str):
    """Every node's text and box."""
    out = []
    for match in re.finditer(r"<node[^>]*>", xml):
        node = match.group(0)
        found = re.search(r'text="([^"]*)"', node)
        box = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if found and box:
            out.append((tuple(int(g) for g in box.groups()), found.group(1)))
    return out


def _long_texts(xml: str):
    """The boxes of the long texts on the screen, with their text.

    A paragraph is a long text: the short ones are labels, and a label
    is never the thing that collides with something else.
    """
    return [(box, text) for box, text in _nodes(xml) if len(text) >= 40]


def touches(a, b) -> bool:
    """Do two boxes overlap -- or even touch?

    Touching counts: the bug this looks for was a description that ran
    right into the status beside it, with no gap at all. Two boxes that
    share an edge are as unreadable as two boxes that overlap.
    """
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _shows(xml: str, needles) -> bool:
    """Does the tree show any of these strings?"""
    shown = texts_in(xml)
    return any(needle.lower() in text.lower()
               for needle in needles for text in shown)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="dist", help="where to write the captures")
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []

    for key, label, expected, forbidden in SCREENS:
        tree = dump_tree()
        if not tree:
            print(f"::error::could not read the accessibility tree for {label}")
            return 1

        # Tap the destination by its own label. EVERY screen, including
        # the first: the app does not open on "Add" -- it opens where the
        # user left off, and with no credentials that is Account auth.
        # Assuming the first screen is the one the app opened on is how
        # you end up asserting that Account auth shows a link field.
        spot = node_bounds(tree, label)
        if spot is None:
            # The destination is off the strip: scroll it and look again.
            swipe_strip(tree)
            tree = dump_tree()
            spot = node_bounds(tree, label)
        if spot is None:
            print(f"::error::no node labelled {label!r} to tap")
            failures.append(f"{label}: no tab to tap")
            continue

        # Tap until the screen actually changes. The first tap after the
        # app comes up is sometimes swallowed -- the window is still
        # animating in, and the tap lands on a surface that is not there
        # yet -- and a test that silently screenshots the PREVIOUS screen
        # is worse than no test, because it looks like it passed.
        changed = False
        for attempt in range(4):
            adb("shell", "input", "tap", str(spot[0]), str(spot[1]))
            time.sleep(1.5)
            tree = dump_tree()
            if _shows(tree, expected):
                changed = True
                break
            # The tap missed: the chip was at the very edge of the
            # strip, so the tap landed on the page next to it. Look for
            # the label again, and scroll the strip if it has gone.
            spot = node_bounds(tree, label)
            if spot is None:
                swipe_strip(tree, towards_end=True)
                tree = dump_tree()
                spot = node_bounds(tree, label)
                if spot is None:
                    break
            else:
                # It is still there, but half off the strip: scroll it
                # into the open and tap it where it really is.
                swipe_strip(tree, towards_end=label_is_at_the_end(tree, label))
                tree = dump_tree()
                spot = node_bounds(tree, label)
                if spot is None:
                    break
        if not changed:
            print(f"::error::tapping {label!r} did not change the screen")
            print(f"  what it shows: {sorted(texts_in(tree))[:20]}")
            failures.append(f"{label}: the tap did not change the screen")
            continue

        (out / f"screen-{key}.xml").write_text(tree, encoding="utf-8")
        shot = out / f"screen-{key}.png"
        with shot.open("wb") as handle:
            proc = subprocess.run(["adb", "exec-out", "screencap", "-p"],
                                  capture_output=True, timeout=120)
            handle.write(proc.stdout)

        shown = texts_in(tree)
        found = [needle for needle in expected
                 if any(needle.lower() in text.lower() for text in shown)]
        if not found:
            print(f"::error::the {label} screen does not show any of {expected}")
            print(f"  what it shows: {sorted(shown)[:20]}")
            failures.append(f"{label}: shows none of {expected}")
        else:
            print(f"  the {label} screen shows {found[0]!r}")
        # The strings that must not be there. A dependency the app does
        # not have is a false alarm, and a false alarm in the list of
        # dependencies is the one thing that makes the whole list
        # untrustworthy.
        for needle in forbidden:
            if any(needle.lower() in text.lower() for text in shown):
                print(f"::error::the {label} screen shows {needle!r}, which it"
                      f" must not: it is not a dependency of this app")
                failures.append(f"{label}: shows {needle!r}")

        # The bottom bar is on every screen and says where the downloads
        # go. A raw absolute path is unreadable on a phone -- it is
        # "/storage/emulated/0/Android/data/<the package name>/..." --
        # so the path is shortened to its last two segments.
        for text in shown:
            if text.startswith(("/storage", "/sdcard", "/data/")):
                print(f"::error::the {label} screen shows a raw path in the"
                      f" bottom bar: {text!r}")
                failures.append(f"{label}: a raw path in the bottom bar")
                break

        # The dependency rows: the description and the status must not be
        # drawn on top of each other. They were: the version and the
        # badge were centred on a paragraph, and the paragraph ran into
        # them.
        #
        # A dependency's description on Android is the long one that
        # talks about the platform, so that is what is looked for: any
        # long text about Android. The two must not touch the status
        # beside them.
        if key == "settings":
            descriptions = [box for box, text in _long_texts(tree)
                            if "android" in text.lower()
                            and not text.startswith("/")]
            statuses = (boxes_of(tree, "not detected")
                        + boxes_of(tree, "MISSING"))
            for status in statuses:
                for description in descriptions:
                    if touches(status, description):
                        print("::error::on the Settings screen the dependency"
                              " description and its status are drawn on top of"
                              " each other")
                        failures.append("settings: the description overlaps"
                                        " the status")
                        break
        if not shot.exists() or shot.stat().st_size == 0:
            failures.append(f"{label}: no screenshot")

    if failures:
        print(f"FAILED: {len(failures)} screen(s): {', '.join(failures)}")
        return 1
    print(f"OK: every screen was visited and shows its own content ({out}/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
