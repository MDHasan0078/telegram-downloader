#!/bin/bash
# Build Android APK via Flet (Flutter toolchain).
# Produces dist/apk/telegram-downloader_<ver>.apk
#
# Requires once: pip install -r requirements-flet.lock ; flet doctor ;
#               accept Android SDK licenses.
# Usage: ./scripts/build-apk.sh [--debug]
#
# Two invariants worth keeping if you edit this:
#   1. build/ is setuptools' staging directory and NOT an output directory.
#      Flet also defaults to build/apk, so its output is moved to dist/apk
#      below: otherwise a later `rm -rf build` and Flet's re-creation of
#      build/apk are the same path, and "clean first" stops being meaningful.
#   2. the toolchain is installed from requirements-flet.lock with
#      --require-hashes. `pip install flet[all]==0.86.5` pins only the
#      top-level name and floats the ~50 distributions under it.
set -euo pipefail
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- clean slate
# `build/` is gitignored: a stale snapshot left by a previous (or hostile)
# checkout can otherwise be staged verbatim into the shipped wheel.
rm -rf build dist
rm -rf ./*.egg-info src/*.egg-info
mkdir -p dist

# ------------------------------------------------------- reproducible builds
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct 2>/dev/null || true)"
fi
if ! printf '%s' "$SOURCE_DATE_EPOCH" | grep -qE '^[0-9]+$'; then
  echo "build-apk.sh: cannot determine SOURCE_DATE_EPOCH (no git history and" >&2
  echo "  SOURCE_DATE_EPOCH unset). Refusing to emit a non-reproducible build." >&2
  exit 1
fi
export SOURCE_DATE_EPOCH
export PYTHONHASHSEED=0
echo "==> SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"

# ------------------------------------------------------------------ toolchain
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$PYV" != "3.12" ]; then
  echo "build-apk.sh: running under CPython $PYV, but requirements-flet.lock" >&2
  echo "  pins CPython 3.12 artifacts (build.yml pins python-version 3.12)." >&2
  echo "  Install Python 3.12 and re-run, or regenerate the lock for $PYV." >&2
  exit 1
fi
for f in requirements-flet.lock requirements-build.lock; do
  [ -f "$f" ] || { echo "build-apk.sh: $f is missing." >&2; exit 1; }
done
echo "==> installing hash-pinned build toolchain"
pip install --require-hashes --only-binary :all: \
  -r requirements-flet.lock -r requirements-build.lock

# ------------------------------------------------------------------- building
# pyaes ships sdist-only; the runtime closure is vendored in wheels/ and
# verified by requirements-runtime.lock. Flet only needs to be able to *see* it.
if [ -f wheels/SHA256SUMS ]; then
  ( cd wheels && sha256sum -c --quiet SHA256SUMS ) || {
    echo "build-apk.sh: vendored wheel checksum mismatch in wheels/" >&2
    exit 1
  }
fi
export PIP_FIND_LINKS="$PWD/wheels"
# Flet shells out to pip for the project's own dependencies. Left at its
# default, pip builds in an isolated env and *downloads a fresh setuptools from
# PyPI*, executing it in this working directory. The pinned setuptools from
# requirements-build.lock is already installed, so isolation buys nothing here
# and only widens the supply chain.
export PIP_NO_BUILD_ISOLATION=0
export PIP_REQUIRE_VIRTUALENV=false

if [ "${1:-}" = "--debug" ]; then
  flet build apk --project telegram_downloader.gui --module-name main --flutter-build-args=--debug --yes
else
  flet build apk --project telegram_downloader.gui --module-name main --yes
fi

# --------------------------------------------- move artifacts out of build/
# Flet leaves its output in build/apk. Copy it into dist/ and delete build/ so
# that `build/` once again means exactly one thing: setuptools' staging area,
# which the next `rm -rf build` at the top of this script can safely nuke.
[ -d build/apk ] || { echo "build-apk.sh: no build/apk after flet build."; ls -R build 2>/dev/null; exit 1; }
mkdir -p dist/apk
find build/apk -maxdepth 1 -name '*.apk' -exec cp {} dist/apk/ \;
rm -rf build
APKS=( dist/apk/*.apk )
if [ "${#APKS[@]}" -ne 1 ]; then
  echo "build-apk.sh: expected exactly 1 APK, found ${#APKS[@]}" >&2
  exit 1
fi
echo "APK at ${APKS[0]}. Copy to phone and install."
echo "Note: first build downloads Flutter + Android SDK (large, one-time)."
