#!/bin/bash
# Build macOS .dmg via Flet. MUST run on macOS with Xcode CLI tools.
# Usage: ./scripts/build-dmg.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" != "Darwin" ]; then
  echo "This script must run on macOS. It cannot build a .dmg from Linux."
  echo "On a Mac: pip install 'flet[all]' && ./scripts/build-dmg.sh"
  exit 1
fi
pip install -U "flet[all]" 2>&1 | tail -2
flet build macos --project telegram_downloader.gui --module-name main
echo "App under build/macos/. Drag to /Applications or package with create-dmg:"
echo "  brew install create-dmg && create-dmg build/*.dmg build/macos/*.app"
