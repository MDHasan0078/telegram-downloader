#!/bin/bash
# Install system + Python deps for Telegram Downloader, per-OS.
# Usage: ./scripts/install-deps.sh [--gui] [--speed]
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
  echo "Unknown OS — install Python 3.9+ and ffmpeg manually."
fi

EXTRAS=""
[ "$GUI" = "1" ] && EXTRAS="$EXTRAS,gui"
[ "$SPEED" = "1" ] && EXTRAS="$EXTRAS,speed"
EXTRAS="$(echo "$EXTRAS" | sed 's/^,//')"
if [ -n "$EXTRAS" ]; then pip install -U ".[$EXTRAS]"; else pip install -U .; fi
echo "==> Done. Try: tg-dl deps   |   tg-dl gui"
