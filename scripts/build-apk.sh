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
