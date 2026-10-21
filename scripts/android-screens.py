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
SCREENS = [
    ("add", "Add", ["Telegram link(s)", "Load .txt"]),
    ("ongoing", "Ongoing", ["Downloads (", "Clear finished",
                            "No downloads in queue"]),
    ("auth", "Account auth", ["Telegram account", "API ID", "Log in"]),
    ("settings", "Settings", ["Dependencies", "Check for updates"]),
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


def texts_in(xml: str) -> set[str]:
    return {t for t in re.findall(r'text="([^"]*)"', xml) if t.strip()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="dist", help="where to write the captures")
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []

    for key, label, expected in SCREENS:
        tree = dump_tree()
        if not tree:
            print(f"::error::could not read the accessibility tree for {label}")
            return 1

        if key != "add":
            # Tap the destination by its own label.
            spot = node_bounds(tree, label)
            if spot is None:
                print(f"::error::no node labelled {label!r} to tap")
                failures.append(f"{label}: no tab to tap")
                continue
            adb("shell", "input", "tap", str(spot[0]), str(spot[1]))
            time.sleep(2.0)
            tree = dump_tree()

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
        if not shot.exists() or shot.stat().st_size == 0:
            failures.append(f"{label}: no screenshot")

    if failures:
        print(f"FAILED: {len(failures)} screen(s): {', '.join(failures)}")
        return 1
    print(f"OK: every screen was visited and shows its own content ({out}/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
