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
for f in requirements-flet.lock requirements-build.lock; do
  [ -f "$f" ] || { echo "build-dmg.sh: $f is missing." >&2; exit 1; }
done
echo "==> installing hash-pinned build toolchain"
pip install --require-hashes --only-binary :all: \
  -r requirements-flet.lock -r requirements-build.lock

# ------------------------------------------------------------------- building
export PIP_FIND_LINKS="$PWD/wheels"
# Flet shells out to pip for the project's own dependencies. Left at its
# default, pip builds in an isolated env and *downloads a fresh setuptools from
# PyPI*, executing it in this working directory. The pinned setuptools from
# requirements-build.lock is already installed, so isolation buys nothing here
# and only widens the supply chain.
export PIP_NO_BUILD_ISOLATION=0
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
