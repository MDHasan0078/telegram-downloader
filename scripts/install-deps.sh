#!/bin/bash
# Install system + Python deps for Telegram Downloader, per-OS.
# Usage: ./scripts/install-deps.sh [--gui] [--speed]
#
# NOTE ON --speed: the `speed` extra (cryptg) is NOT installed by default and is
# not part of requirements-runtime.lock. cryptg is an optional Telethon C
# acceleration module — Telethon works correctly without it — and it publishes
# an sdist only, so installing it means compiling C in the user's own venv. On
# Alpine/Termux/musl that is where pip ends up fetching and running a build
# backend at first launch. Ask for it only if you actually want the speedup and
# understand that it compiles locally.
set -euo pipefail
GUI=0; SPEED=0
for a in "$@"; do case "$a" in --gui) GUI=1;; --speed) SPEED=1;; esac; done

have() { command -v "$1" >/dev/null 2>&1; }
PREFIX="${PREFIX:-}"

if [ -n "$PREFIX" ] && echo "$PREFIX" | grep -q com.termux; then
  echo "==> Termux/Android detected"
  pkg update && pkg install -y python ffmpeg
elif [ "$(uname -s)" = "Darwin" ]; then
  echo "==> macOS detected"
  if ! have brew; then echo "Install Homebrew first: https://brew.sh"; exit 1; fi
  # Not `|| true`. brew exits non-zero for real, actionable failures (a formula
  # with no bottle for this macOS, a half-finished upgrade, a broken tap), and
  # swallowing them produced a script that reported success and then failed
  # somewhere unrelated minutes later, inside pip. The remediation hint is
  # printed instead of a silent continuation, because the common benign cause
  # of a non-zero brew is "already installed but not linked".
  brew install python ffmpeg || {
    echo "install-deps.sh: 'brew install python ffmpeg' failed." >&2
    echo "  If both are already installed but unlinked, run:" >&2
    echo "    brew link --overwrite python ffmpeg" >&2
    echo "  and re-run this script." >&2
    exit 1
  }
elif have apt-get; then
  echo "==> Debian/Ubuntu detected"
  sudo apt update
  sudo apt install -y python3 python3-venv python3-pip ffmpeg
elif have dnf; then
  sudo dnf install -y python3 ffmpeg
elif have pacman; then
  sudo pacman -Sy --noconfirm python ffmpeg
else
  echo "Unknown OS — install Python 3.10+ and ffmpeg manually."
fi

EXTRAS=""
[ "$GUI" = "1" ] && EXTRAS="$EXTRAS,gui"
[ "$SPEED" = "1" ] && EXTRAS="$EXTRAS,speed"
EXTRAS="$(echo "$EXTRAS" | sed 's/^,//')"

# ------------------------------------------------- vendored wheels are the source
# pyaes is a Telethon dependency that PyPI publishes as an sdist ONLY, so any
# `--only-binary` install fails without a local wheel to fall back on. That is
# the reason wheels/ is consulted at all; it is NOT a reason to trust it, so it
# is verified here first. An unverified find-links directory is worse than no
# find-links at all: pip will happily select a wheel nothing ever hashed.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELDIR="$REPO_ROOT/wheels"
[ -d "$WHEELDIR" ] && [ -f "$WHEELDIR/SHA256SUMS" ] || {
  echo "install-deps.sh: $WHEELDIR/ and $WHEELDIR/SHA256SUMS are both required." >&2
  echo "  They are the vendored runtime closure. Fetch the repo with its wheels/" >&2
  echo "  (they are tracked on purpose -- see .gitignore) and re-run." >&2
  exit 1
}
[ -f "$REPO_ROOT/requirements-runtime.lock" ] || {
  echo "install-deps.sh: requirements-runtime.lock is missing; without it the" >&2
  echo "  install below would resolve floating versions with nothing to check" >&2
  echo "  them against." >&2
  exit 1
}
echo "==> verifying vendored wheels against wheels/SHA256SUMS"
( cd "$WHEELDIR" && sha256sum -c --quiet SHA256SUMS ) || {
  echo "install-deps.sh: vendored wheel checksum mismatch in $WHEELDIR/" >&2
  exit 1
}
# Two-way completeness, same reason as scripts/build-deb.sh: with
# PIP_FIND_LINKS set, a wheel nobody hashed is still selectable by pip.
UNMANIFESTED="$( cd "$WHEELDIR" && comm -13 \
  <(awk '{print $2}' SHA256SUMS | sort) <(ls -1 ./*.whl 2>/dev/null | sed 's#^\./##' | sort) )"
[ -z "$UNMANIFESTED" ] || {
  echo "install-deps.sh: wheels present but absent from SHA256SUMS:" >&2
  printf '  %s\n' $UNMANIFESTED >&2
  exit 1
}
MISSING_WHEEL="$( cd "$WHEELDIR" && comm -23 \
  <(awk '{print $2}' SHA256SUMS | sort) <(ls -1 ./*.whl 2>/dev/null | sed 's#^\./##' | sort) )"
[ -z "$MISSING_WHEEL" ] || {
  echo "install-deps.sh: SHA256SUMS references files that are not present:" >&2
  printf '  %s\n' $MISSING_WHEEL >&2
  exit 1
}
export PIP_FIND_LINKS="$WHEELDIR"

# --------------------------------------------------------------- build backend
# --no-build-isolation below means the backend has to already be importable, and
# Python >= 3.12's ensurepip does not ship setuptools at all. Install the pinned
# one rather than letting pip resolve `setuptools>=68` off the index.
#
# Unconditional, on purpose. This used to be guarded by `if ! python3 -c 'import
# setuptools'`, which was backwards: an importable setuptools is the *normal*
# state on a dev machine, so the pin was skipped exactly where it mattered and
# the editable install was driven by whatever version happened to be in the
# ambient interpreter.
[ -f "$REPO_ROOT/requirements-build.lock" ] || {
  echo "install-deps.sh: requirements-build.lock is missing." >&2
  echo "  Without it pip would fetch an unpinned build backend and execute it." >&2
  exit 1
}
echo "==> installing pinned build backend"
pip install --require-hashes --only-binary :all: -r "$REPO_ROOT/requirements-build.lock"

# ------------------------------------------------------------------ resolution
# `--require-hashes` cannot be used on `pip install -e .`: pip refuses outright
# ("the editable requirement ... cannot be installed when requiring hashes,
# because there is no single file to hash" -- verified against pip 25.0.1), and
# a local directory has no published digest to compare against. The strongest
# correct alternative is therefore a hard `--no-index --find-links wheels/`,
# which makes every candidate artifact come out of a directory whose contents
# were just checksum-verified and whose pins are exact `==` in
# pyproject.toml + requirements-runtime.lock.
#
# --no-index is only possible where wheels/ actually covers this interpreter.
# wheels/ carries msgpack for manylinux x86_64 only, so on aarch64/Termux it
# cannot be satisfied and the honest thing is to say so and keep going, rather
# than break the platforms this script explicitly supports. The probe below is
# pip's own resolver, so it answers the real question for the real machine.
HERMETIC=0
if [ "$SPEED" = "1" ]; then
  # cryptg is sdist-only for CPython and is deliberately excluded from
  # requirements-runtime.lock, so it can never be satisfied offline. Opting into
  # compiling C from an index-sourced sdist is what --speed means.
  echo "==> --speed: cryptg is sdist-only and not vendored, so this install"
  echo "    CANNOT be hermetic. cryptg will be compiled from a PyPI sdist."
else
  if pip install --dry-run --no-index --find-links "$WHEELDIR" \
       --require-hashes --only-binary :all: \
       -r "$REPO_ROOT/requirements-runtime.lock" >/dev/null 2>&1; then
    HERMETIC=1
    echo "==> vendored wheels cover this interpreter: installing offline"
  else
    echo "install-deps.sh: WARNING - wheels/ cannot satisfy this interpreter" >&2
    echo "  (wheels/ carries msgpack for manylinux x86_64 only; aarch64, musl" >&2
    echo "  and other ABIs are not covered). Falling back to PyPI." >&2
    echo "  The resulting install is NOT hash-verified. For a hermetic install" >&2
    echo "  use the .deb, or run this on an x86_64 machine." >&2
  fi
fi

PIPFLAGS="--no-build-isolation"
if [ "$HERMETIC" = "1" ]; then
  # --no-index for the editable install too, so nothing can be re-resolved from
  # an index behind the probe's back. --no-deps because the closure above is
  # exactly requirements-runtime.lock; re-resolving would only reintroduce the
  # float this is meant to remove.
  PIPFLAGS="$PIPFLAGS --no-index --find-links $WHEELDIR --only-binary :all: --no-deps"
elif [ "$SPEED" = "1" ]; then
  # cryptg compiles from sdist: no --only-binary, and --no-deps so pip does not
  # re-resolve cryptg off the index a second time.
  PIPFLAGS="$PIPFLAGS --no-deps"
  echo "==> installing cryptg from its PyPI sdist (compiles locally; needs a C toolchain)"
  pip install "$PIPFLAGS" "cryptg>=0.4.0,<2" || {
    echo "install-deps.sh: cryptg failed to build; drop --speed to continue." >&2
    exit 1
  }
else
  PIPFLAGS="$PIPFLAGS --only-binary :all:"
fi
if [ -n "$EXTRAS" ]; then
  # shellcheck disable=SC2086
  pip install $PIPFLAGS -e ".[$EXTRAS]"
else
  # shellcheck disable=SC2086
  pip install $PIPFLAGS -e .
fi
echo "==> Done. Try: tg-dl deps   |   tg-dl gui"
