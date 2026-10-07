#!/usr/bin/env python3
"""Fail the build if a built wheel's code is not byte-identical to src/.

setuptools stages ``src/`` into ``build/lib/`` with ``update=1``, which *skips*
a ``build/lib`` file whose mtime is newer than its ``src/`` twin. ``build/`` is
gitignored, so a stale (or hostile) ``build/lib/`` survives across checkouts and
gets staged into the shipped wheel verbatim -- import-time code execution in
every user's install, from code that is not in git.

This script closes that hole from the outside: it compares the *built artifact*
against the *source of truth*, so it does not matter what ``build/`` contained.

Checks, all of which are hard failures:

  1. every ``<package>/**.py`` member in the wheel exists in ``src/``
     (catches a module that only exists in ``build/lib/``);
  2. every ``<package>/**.py`` file in ``src/`` exists in the wheel
     (catches a module the staging step silently dropped);
  3. each of those pairs is byte-identical.

Stdlib only, so it runs in CI before anything is installed.

Usage:
    scripts/verify_wheel_matches_src.py dist/telegram_downloader-1.2.0-py3-none-any.whl
    scripts/verify_wheel_matches_src.py <wheel> --src src --package telegram_downloader
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

DEFAULT_PACKAGE = "telegram_downloader"
# Only real source modules. __pycache__/*.pyc is not ".py" and setuptools never
# stages it into a wheel; *.pyi/py.typed are optional and absent here.
SUFFIX = ".py"


def _fail(problems: list[str], summary: list[str]) -> None:
    print("verify_wheel_matches_src: FAILED", file=sys.stderr)
    for line in summary:
        print(f"  {line}", file=sys.stderr)
    for line in problems:
        print(f"  - {line}", file=sys.stderr)
    print(
        "\nThis means the wheel does not correspond to src/. Almost always a\n"
        "stale build/lib/ (gitignored, never cleaned). The build scripts are\n"
        "supposed to `rm -rf build dist *.egg-info` before packaging; if you\n"
        "are calling a build backend by hand, do the same before retrying.",
        file=sys.stderr,
    )
    sys.exit(1)


def _first_diff(a: bytes, b: bytes) -> str:
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            line = a.count(b"\n", 0, i) + 1
            return (
                f"first difference at byte {i} (line {line}): "
                f"wheel=0x{a[i]:02x} src=0x{b[i]:02x} "
                f"(wheel {len(a)} bytes, src {len(b)} bytes)"
            )
    return f"length differs: wheel {len(a)} bytes, src {len(b)} bytes"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wheel", type=Path, help="path to the built .whl")
    ap.add_argument("--src", type=Path, default=Path("src"), help="source root (default: src)")
    ap.add_argument("--package", default=DEFAULT_PACKAGE, help=f"package to check (default: {DEFAULT_PACKAGE})")
    args = ap.parse_args(argv)

    wheel: Path = args.wheel
    if not wheel.is_file():
        print(f"verify_wheel_matches_src: no such wheel: {wheel}", file=sys.stderr)
        return 1

    src_pkg = args.src / args.package
    if not src_pkg.is_dir():
        print(f"verify_wheel_matches_src: no such source package dir: {src_pkg}", file=sys.stderr)
        return 1

    src_files = {
        p.relative_to(args.src).as_posix(): p.read_bytes()
        for p in sorted(src_pkg.rglob(f"*{SUFFIX}"))
        if "__pycache__" not in p.parts
    }

    if not zipfile.is_zipfile(wheel):
        print(f"verify_wheel_matches_src: {wheel} is not a zip/wheel", file=sys.stderr)
        return 1

    problems: list[str] = []
    summary: list[str] = []
    packaged: dict[str, bytes] = {}
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/") or not name.endswith(SUFFIX):
                continue
            # Only the top-level import package, not .dist-info or vendored
            # dependencies: "<pkg>/" or "<pkg>/<sub>/..." with nothing above.
            head = name.split("/", 1)[0]
            if head != args.package:
                continue
            packaged[name] = zf.read(name)

    if not packaged:
        print(
            f"verify_wheel_matches_src: {wheel} contains no {args.package}/**{SUFFIX} members",
            file=sys.stderr,
        )
        return 1

    for name in sorted(set(packaged) - set(src_files)):
        problems.append(f"in wheel but not in src/ (stale build/lib or extra module): {name}")
    for name in sorted(set(src_files) - set(packaged)):
        problems.append(f"in src/ but not packaged in wheel (module silently dropped): {name}")
    for name in sorted(set(packaged) & set(src_files)):
        if packaged[name] != src_files[name]:
            problems.append(f"content mismatch {name}: {_first_diff(packaged[name], src_files[name])}")

    if problems:
        _fail(problems, summary)
        return 1  # unreachable, _fail exits

    total = sum(len(b) for b in packaged.values())
    print(
        f"verify_wheel_matches_src: OK - {wheel.name}: "
        f"{len(packaged)} {args.package} module(s), {total} bytes, "
        f"all byte-identical to {src_pkg}/"
    )
    for name in sorted(packaged):
        print(f"  ok {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
