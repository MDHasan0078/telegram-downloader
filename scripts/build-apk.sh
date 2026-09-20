#!/bin/bash
# Build Android APK via Flet (Flutter toolchain).
# Requires once: pip install 'flet[all]' ; flet doctor ; accept Android SDK licenses.
# Usage: ./scripts/build-apk.sh [--debug]
set -euo pipefail
cd "$(dirname "$0")/.."
# pyaes ships sdist-only; flet's packager uses --only-binary :all:,
# so expose our vendored wheel via PIP_FIND_LINKS.
export PIP_FIND_LINKS="$PWD/wheels"
# Fail fast if a vendored wheel was corrupted or swapped between commits.
if [ -f wheels/SHA256SUMS ]; then
  ( cd wheels && sha256sum -c SHA256SUMS ) || {
    echo "build-apk.sh: vendored wheel checksum mismatch in wheels/" >&2
    exit 1
  }
fi
pip install -U "flet[all]==0.86.5"
if [ "${1:-}" = "--debug" ]; then
  flet build apk --project telegram_downloader.gui --module-name main --flutter-build-args=--debug --yes
else
  flet build apk --project telegram_downloader.gui --module-name main --yes
fi
echo "APK should be under build/apk/. Copy to phone and install."
echo "Note: first build downloads Flutter + Android SDK (large, one-time)."
