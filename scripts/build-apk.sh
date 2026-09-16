#!/bin/bash
# Build Android APK via Flet (Flutter toolchain).
# Requires once: pip install 'flet[all]' ; flet doctor ; accept Android SDK licenses.
# Usage: ./scripts/build-apk.sh [--debug]
set -euo pipefail
cd "$(dirname "$0")/.."
pip install -U "flet[all]==0.86.5" 2>&1 | tail -2
if [ "${1:-}" = "--debug" ]; then
  flet build apk --project telegram_downloader.gui --module-name main --flutter-build-args=--debug
else
  flet build apk --project telegram_downloader.gui --module-name main
fi
echo "APK should be under build/apk/. Copy to phone and install."
echo "Note: first build downloads Flutter + Android SDK (large, one-time)."
