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
  brew install python ffmpeg || true
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

# Point pip at the vendored wheels/ before anything else. pyaes is a Telethon
# dependency that PyPI publishes as an sdist ONLY, so any `--only-binary` install
# fails without a local wheel to fall back on. Using the vendored copy also means
# this install resolves to the exact artifacts requirements-runtime.lock pins.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$REPO_ROOT/wheels" ]; then
  export PIP_FIND_LINKS="$REPO_ROOT/wheels"
fi

# --no-build-isolation below means the backend has to already be importable, and
# Python >= 3.12's ensurepip does not ship setuptools at all. Install the pinned
# one rather than letting pip resolve `setuptools>=68` off the index.
if ! python3 -c 'import setuptools' >/dev/null 2>&1; then
  if [ -f "$REPO_ROOT/requirements-build.lock" ]; then
    echo "==> installing pinned build backend"
    pip install --require-hashes --only-binary :all: -r "$REPO_ROOT/requirements-build.lock"
  else
    echo "install-deps.sh: requirements-build.lock is missing and setuptools is not" >&2
    echo "  installed; without it pip would fetch an unpinned build backend." >&2
    exit 1
  fi
fi

# The project itself is installed from a local directory, so the only thing that
# reaches an index here is its dependency set; `--no-build-isolation` keeps pip
# from downloading and running a build backend to install a local tree.
PIPFLAGS="--no-build-isolation"
if [ "$SPEED" = "1" ]; then
  # cryptg is sdist-only for CPython, so it cannot satisfy --only-binary and
  # will be compiled locally. Opting into that is the point of --speed.
  echo "==> installing with gui+speed (cryptg compiles from sdist; needs a C toolchain)"
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
