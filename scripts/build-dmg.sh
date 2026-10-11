#!/bin/bash
# Build macOS .app bundle via Flet. MUST run on macOS with Xcode CLI tools.
# Produces dist/macos/Telegram Downloader.app
# Usage: ./scripts/build-dmg.sh
#
# Signing/notarization is NOT done here: it needs a Developer ID certificate and
# an Apple notarization key, which only exist as repository secrets. .github/
# workflows/build.yml runs the codesign/notarytool/stapler steps after this
# script, and only when those secrets are present.
set -euo pipefail
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- clean slate
# `build/` is gitignored and doubles as setuptools' staging directory, so a
# stale snapshot must never be around when packaging runs.
rm -rf build dist
rm -rf ./*.egg-info src/*.egg-info
mkdir -p dist

# ------------------------------------------------------- reproducible builds
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct 2>/dev/null || true)"
fi
if ! printf '%s' "$SOURCE_DATE_EPOCH" | grep -qE '^[0-9]+$'; then
  echo "build-dmg.sh: cannot determine SOURCE_DATE_EPOCH (no git history and" >&2
  echo "  SOURCE_DATE_EPOCH unset). Refusing to emit a non-reproducible build." >&2
  exit 1
fi
export SOURCE_DATE_EPOCH
export PYTHONHASHSEED=0
echo "==> SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This script must run on macOS. It cannot build a .dmg from Linux."
  echo "On a Mac: pip install -r requirements-flet.lock && ./scripts/build-dmg.sh"
  exit 1
fi

# ------------------------------------------------------------------ toolchain
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$PYV" != "3.12" ]; then
  echo "build-dmg.sh: running under CPython $PYV, but requirements-flet.lock" >&2
  echo "  pins CPython 3.12 artifacts (build.yml pins python-version 3.12)." >&2
  echo "  Install Python 3.12 and re-run, or regenerate the lock for $PYV." >&2
  exit 1
fi
# requirements-runtime.lock is in this list because the cross-check below reads
# it: without it there is no second opinion on which telethon the .deb ships.
for f in requirements-flet.lock requirements-build.lock requirements-runtime.lock; do
  [ -f "$f" ] || { echo "build-dmg.sh: $f is missing." >&2; exit 1; }
done
echo "==> installing hash-pinned build toolchain"
pip install --require-hashes --only-binary :all: \
  -r requirements-flet.lock -r requirements-build.lock

# ----------------------------------------------------- vendored wheel trust root
# This job had no wheel verification at all, which is the wrong direction: the
# .deb job refuses to build without it, and the sibling .apk job at least had a
# conditional one. PIP_FIND_LINKS below turns wheels/ into an install SOURCE for
# the dependency resolution serious_python performs, so an unverified wheel in
# here is a wheel this build can select.
#
# The block is unconditional, for the reason it is unconditional in the sibling
# script: a check wrapped in `if [ -f wheels/SHA256SUMS ]` can be switched off by
# deleting one file, which is not a check.
SELF="$(basename "$0")"
WHEELDIR="wheels"
[ -d "$WHEELDIR" ] && [ -f "$WHEELDIR/SHA256SUMS" ] || {
  echo "$SELF: $WHEELDIR/ and $WHEELDIR/SHA256SUMS are both required." >&2
  echo "  They are the vendored runtime closure, and PIP_FIND_LINKS below" >&2
  echo "  installs from them. A missing manifest means no build, not a skip." >&2
  exit 1
}
echo "==> verifying vendored wheels against $WHEELDIR/SHA256SUMS"
( cd "$WHEELDIR" && sha256sum -c --quiet SHA256SUMS ) || {
  echo "$SELF: vendored wheel checksum mismatch in $WHEELDIR/" >&2
  exit 1
}
# Two-way completeness, for the same reason scripts/build-deb.sh does it: with
# PIP_FIND_LINKS set, pip will happily select a wheel the manifest never mentions,
# so "every listed wheel verified" is not "every available wheel verified".
UNMANIFESTED="$( cd "$WHEELDIR" && comm -13 \
  <(awk '{print $2}' SHA256SUMS | sort) <(ls -1 ./*.whl 2>/dev/null | sed 's#^\./##' | sort) )"
[ -z "$UNMANIFESTED" ] || {
  echo "$SELF: wheels present but absent from SHA256SUMS:" >&2
  printf '  %s\n' $UNMANIFESTED >&2
  exit 1
}
MISSING_WHEEL="$( cd "$WHEELDIR" && comm -23 \
  <(awk '{print $2}' SHA256SUMS | sort) <(ls -1 ./*.whl 2>/dev/null | sed 's#^\./##' | sort) )"
[ -z "$MISSING_WHEEL" ] || {
  echo "$SELF: SHA256SUMS references files that are not present:" >&2
  printf '  %s\n' $MISSING_WHEEL >&2
  exit 1
}

# The one dependency this build cannot pin for itself. `flet build macos` does
# not install this project with pip; it hands pyproject's dependency list to
# serious_python, which resolves it against PyPI at build time with no
# --require-hashes. Assert the three artifacts agree on the version so the .app
# cannot silently differ from the .deb built from the same commit.
python3 - "$SELF" <<'PY' || exit 1
import re, sys, pathlib, glob
script = sys.argv[1]
def die(msg):
    sys.stderr.write(f"{script}: {msg}\n")
    sys.exit(1)
m = re.search(r'^\s*"telethon\s*==\s*([0-9][^"]*)"',
              pathlib.Path("pyproject.toml").read_text(), re.M)
if not m:
    die("pyproject.toml must pin telethon with an exact ==.\n"
        "  `flet build macos` hands this list to serious_python, which resolves it\n"
        "  against PyPI with no --require-hashes, so a range is a floating version\n"
        "  that can differ from the .deb built from the same commit.")
pinned = m.group(1)
lm = re.search(r"^telethon==([^\s\\]+)",
               pathlib.Path("requirements-runtime.lock").read_text(), re.M)
if not lm:
    die("requirements-runtime.lock pins no telethon; cannot cross-check.")
if lm.group(1) != pinned:
    die(f"pyproject.toml pins telethon=={pinned} but requirements-runtime.lock "
        f"pins telethon=={lm.group(1)}. The .deb and the .app would ship "
        f"different code from one commit.")
found = glob.glob(f"wheels/telethon-{pinned}-*.whl")
if len(found) != 1:
    die(f"wheels/ holds {len(found)} telethon=={pinned} wheels, expected 1 "
        f"(vendored set is out of sync with pyproject.toml).")
print(f"==> telethon=={pinned} agrees across pyproject.toml, "
      f"requirements-runtime.lock and wheels/")
PY

# ------------------------------------------------------------------- building
# PIP_FIND_LINKS is load-bearing, not decorative. pyaes (a Telethon dependency)
# is published as an sdist ONLY, so the vendored copy is the only artifact that
# can satisfy this closure without compiling C.
#
# It does NOT make the install hermetic. The env var does reach the pip that
# serious_python spawns -- flet merges os.environ, and Dart's Process.start
# defaults to includeParentEnvironment:true -- but PIP_FIND_LINKS is *additive*
# to the index: everything else still comes from PyPI, with no hash check.
# `--no-index` is not an option here either: wheels/ carries manylinux x86_64
# artifacts and no macOS arm64 wheel for msgpack, which the Flet GUI imports, so
# `--no-index` fails outright on a macOS runner. The exact telethon== pin above
# is what closes what can be closed without a serious_python change.
export PIP_FIND_LINKS="$PWD/wheels"
# Flet shells out to pip for the project's own dependencies. Left at its
# default, pip builds in an isolated env and *downloads a fresh setuptools from
# PyPI*, executing it in this working directory. The pinned setuptools from
# requirements-build.lock is already installed, so isolation buys nothing here
# and only widens the supply chain.
export PIP_NO_BUILD_ISOLATION=0

# Same stale-cache problem as build-apk.sh, and the same fix: the macOS runner
# image carries a pre-warmed pub cache that can pin an older jni than the
# bindings Flet generates require. See the longer note in build-apk.sh.
rm -rf "${PUB_CACHE:-$HOME/.pub-cache}"

flet build macos --project telegram_downloader.gui --module-name main --yes

# --------------------------------------------- move artifacts out of build/
[ -d build/macos ] || { echo "build-dmg.sh: no build/macos after flet build."; ls -R build 2>/dev/null; exit 1; }
mkdir -p dist/macos
cp -R build/macos/. dist/macos/
rm -rf build
APPS=( dist/macos/*.app )
if [ "${#APPS[@]}" -ne 1 ]; then
  echo "build-dmg.sh: expected exactly 1 .app bundle, found ${#APPS[@]}" >&2
  exit 1
fi
echo "App at ${APPS[0]}"
echo "Package it with the pinned create-dmg (see build.yml):"
echo "  create-dmg --volname 'Telegram Downloader' out.dmg '$APP'"
