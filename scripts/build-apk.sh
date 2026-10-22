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
#   3. the vendored wheel set is verified UNCONDITIONALLY. Flet does not run
#      pip over this project -- it hands pyproject's dependency list to
#      serious_python, which resolves it against PyPI with no hash check. That
#      resolution cannot be closed from here (see the PIP_FIND_LINKS comment
#      below), so what is left is to pin the version and to make every artifact
#      built from one commit agree on it.
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
# requirements-runtime.lock is in this list because the cross-check below reads
# it: without it there is no second opinion on which telethon the .deb ships.
for f in requirements-flet.lock requirements-build.lock requirements-runtime.lock; do
  [ -f "$f" ] || { echo "build-apk.sh: $f is missing." >&2; exit 1; }
done
echo "==> installing hash-pinned build toolchain"
pip install --require-hashes --only-binary :all: \
  -r requirements-flet.lock -r requirements-build.lock

# ----------------------------------------------------- vendored wheel trust root
# PIP_FIND_LINKS below turns wheels/ into an install SOURCE for the dependency
# resolution serious_python performs, so it is verified before anything reads
# it. This block is unconditional on purpose: it used to be wrapped in
# `if [ -f wheels/SHA256SUMS ]`, which meant deleting a single file silently
# removed every check. A guard that an unprivileged checkout edit can switch off
# is not a guard, and this is the same trust root scripts/build-deb.sh refuses
# to build without.
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
# PIP_FIND_LINKS set, pip will happily select a wheel that the manifest never
# mentions, so "every listed wheel verified" does not mean "every available
# wheel verified".
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

# The one dependency this build cannot pin for itself. `flet build apk` does
# not install this project with pip; it hands pyproject's dependency list to
# serious_python, which resolves it against PyPI at build time with no
# --require-hashes. Assert the three artifacts agree on the version so the .apk
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
        "  `flet build apk` hands this list to serious_python, which resolves it\n"
        "  against PyPI with no --require-hashes, so a range is a floating\n"
        "  version that can differ from the .deb built from the same commit.")
pinned = m.group(1)
lm = re.search(r"^telethon==([^\s\\]+)",
               pathlib.Path("requirements-runtime.lock").read_text(), re.M)
if not lm:
    die("requirements-runtime.lock pins no telethon; cannot cross-check.")
if lm.group(1) != pinned:
    die(f"pyproject.toml pins telethon=={pinned} but requirements-runtime.lock "
        f"pins telethon=={lm.group(1)}. The .deb and the .apk would ship "
        f"different code from one commit.")
found = glob.glob(f"wheels/telethon-{pinned}-*.whl")
if len(found) != 1:
    die(f"wheels/ holds {len(found)} telethon=={pinned} wheels, expected 1 "
        f"(vendored set is out of sync with pyproject.toml).")
print(f"==> telethon=={pinned} agrees across pyproject.toml, "
      f"requirements-runtime.lock and wheels/")
PY

# ------------------------------------------------------------------- building
# PIP_FIND_LINKS is load-bearing, not decorative: pyaes (a Telethon dependency)
# is published as an sdist ONLY, and serious_python passes `--only-binary :all:`
# for Android, so without this the build cannot install pyaes at all.
#
# It does NOT, however, make the install hermetic. The env var is inherited by
# the pip that serious_python spawns (verified: flet merges os.environ, Dart's
# Process.start defaults to includeParentEnvironment:true), but PIP_FIND_LINKS
# is *additive* to the index -- everything else still comes from PyPI, unhash-
# checked. `--no-index` cannot be used here: wheels/ holds manylinux x86_64
# wheels, and pip is run under a sitecustomize that reports the platform as
# android-24-<abi>, so msgpack has no compatible vendored artifact and
# `--no-index` fails outright. The exact telethon== pin above is what closes
# what CAN be closed without a serious_python change.
export PIP_FIND_LINKS="$PWD/wheels"
# Flet shells out to pip for the project's own dependencies. Left at its
# default, pip builds in an isolated env and *downloads a fresh setuptools from
# PyPI*, executing it in this working directory. The pinned setuptools from
# requirements-build.lock is already installed, so isolation buys nothing here
# and only widens the supply chain.
export PIP_NO_BUILD_ISOLATION=0
export PIP_REQUIRE_VIRTUALENV=false

# ------------------------------------------------------- stage a lean app dir
# `flet build apk` copies the whole python_app_path into the APK as
# app.zip, and the default app path is the repository root. That shipped
# the entire git history (2.6 MB of .git/), the Linux x86_64 wheel set
# (2.3 MB, useless on Android), the test suite and the screenshot folder
# -- 5.3 MB of the APK that no phone could use.
#
# Only the app itself is staged: the entry point, the package, the
# pyproject that carries [tool.flet], and the licence. PIP_FIND_LINKS
# above still points at the real wheels/ directory, so the pyaes wheel
# that serious_python needs is still found.
STAGE="$PWD/.apk-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp main.py pyproject.toml LICENSE "$STAGE/"
cp -r src "$STAGE/src"
rm -rf "$STAGE"/src/*.egg-info
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ------------------------------------------------------------------ the icon
# The app's icon is an SVG (src/telegram_downloader/assets/icon.svg) and
# flet CANNOT use one: its icon generator drops vector formats and falls
# back to the Flutter default. So the APK shipped with Flutter's logo on
# the home screen, which is the worst icon an app can have.
#
# It is rasterised here, at build time, from the SVG, so the SVG stays
# the one source of the icon and the PNG can never drift from it. If the
# rasteriser is missing the build FAILS rather than shipping the default
# one: this is the package's actual icon, not a nicety.
stage_assets() {
  local dest="$1/assets"
  mkdir -p "$dest"
  if ! command -v rsvg-convert >/dev/null 2>&1; then
    echo "build-apk.sh: rsvg-convert is missing (the package is librsvg2-bin)." >&2
    echo "  The app's icon is an SVG and flet cannot use one, so without" >&2
    echo "  this the APK ships with Flutter's default icon." >&2
    exit 1
  fi
  rsvg-convert -w 512 -h 512 src/telegram_downloader/assets/icon.svg \
    -o "$dest/icon.png"
  echo "==> the icon: staged assets/icon.png (512x512, from the SVG)"
}
stage_assets "$STAGE"
echo "==> staged app dir: $(du -sh "$STAGE" | cut -f1) (the repo root is $(du -sh . | cut -f1))"

# ------------------------------------------------------------- one ABI only
# The APK shipped three ABIs: arm64-v8a (every phone from 2017 on),
# x86_64 (emulators only) and armeabi-v7a (32-bit phones). The other two
# were 80.6 MB of the 153 MB, and they are dead weight on a device that
# can only ever run one of them.
#
# arm64-v8a is the only ABI that matters for a phone, so that is the one
# that ships. The emulator still needs an x86_64 APK, and an arm64-only
# package cannot be installed on an x86_64 emulator -- so a second,
# x86_64-only build is produced for CI, into its own directory, and never
# near the artifact people download.
APK_ARCH="${APK_ARCH:-arm64-v8a}"
# Set to 1 to also build the x86_64 APK the emulator job installs.
APK_EMULATOR_ARCH="${APK_EMULATOR_ARCH:-x86_64}"

# --output puts the artifact back at build/apk, where the rest of this
# script expects it, even though the app path is now the staged copy.
# (The option is --output; flet's CLI does not accept --output-dir here.)
#
# --product is the DISPLAY name: the name under the app's icon on the
# home screen, the window title and the about box. Without it the name
# falls back to --project, which is the Python module
# ("telegram_downloader.gui") -- so the app on a phone was called
# "Telegram_download.gy", which is a developer's file name and not
# something a person should have to read.
if [ "${1:-}" = "--debug" ]; then
  flet build apk "$STAGE" --project telegram_downloader.gui --module-name main \
    --product "Telegram Downloader" \
    --arch "$APK_ARCH" --output "$PWD/build/apk" \
    --flutter-build-args=--debug --yes
else
  flet build apk "$STAGE" --project telegram_downloader.gui --module-name main \
    --product "Telegram Downloader" \
    --arch "$APK_ARCH" --output "$PWD/build/apk" --yes
fi
rm -rf "$STAGE"

# The emulator's APK, if asked for. The Flutter/Gradle caches are warm from
# the build above, so this is a rebuild of one ABI rather than a second
# cold build.
if [ "${APK_BUILD_EMULATOR:-0}" = "1" ]; then
  echo "==> building the $APK_EMULATOR_ARCH APK for the emulator"
  rm -rf "$STAGE"
  mkdir -p "$STAGE"
  cp main.py pyproject.toml LICENSE "$STAGE/"
  cp -r src "$STAGE/src"
  rm -rf "$STAGE"/src/*.egg-info
  find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  stage_assets "$STAGE"
  flet build apk "$STAGE" --project telegram_downloader.gui \
    --module-name main --product "Telegram Downloader" \
    --arch "$APK_EMULATOR_ARCH" \
    --output "$PWD/build/apk-emulator" --yes
  rm -rf "$STAGE"
  mkdir -p dist/apk-emulator
  find build/apk-emulator -maxdepth 1 -name '*.apk' -exec cp {} dist/apk-emulator/ \;
  rm -rf build/apk-emulator
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
