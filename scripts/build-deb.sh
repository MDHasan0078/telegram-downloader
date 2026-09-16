#!/bin/bash
# Build a .deb for Debian/Ubuntu (Linux target).
# Produces dist/telegram-downloader_<ver>_amd64.deb
# Requires: python3, pip, dpkg-deb. Run on Linux.
set -euo pipefail
cd "$(dirname "$0")/.."
# tomllib needs py3.11+; parse with regex so py3.9/3.10 don't silently
# fall back to a wrong 0.1.0 version (fail loudly instead).
# NOTE: print() MUST come before sys.exit() — sys.exit raises SystemExit
# immediately, so any print after it is dead code (VER would be empty).
VER="$(python3 -c '
import re, sys
m = re.search(r"^version\s*=\s*\"([^\"]+)\"", open("pyproject.toml").read(), re.M)
if not m:
    sys.stderr.write("build-deb.sh: cannot find version in pyproject.toml\n")
    sys.exit(1)
print(m.group(1))
')"
# Reject obviously-bad versions (empty / unparsable) with a loud build error.
if ! printf '%s' "$VER" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "build-deb.sh: invalid version in pyproject.toml: '$VER'" >&2
  exit 1
fi
# Verify the vendored wheel against its recorded manifest so a corrupted
# wheels/ copy fails at build time, not mid-install for end users.
if [ -f wheels/SHA256SUMS ]; then
  ( cd wheels && sha256sum -c SHA256SUMS ) || {
    echo "build-deb.sh: vendored wheel checksum mismatch in wheels/" >&2
    exit 1
  }
fi
PKG="dist/debroot"
rm -rf dist "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/telegram-downloader" "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps"

cat > "$PKG/DEBIAN/control" <<EOF
Package: telegram-downloader
Version: $VER
Section: video
Priority: optional
Architecture: amd64
Depends: python3, python3-venv, python3-pip, ffmpeg
Maintainer: tgdownloader <noreply@example.com>
Description: Telegram media downloader (GUI + CLI)
 Persistent login, resumable downloads, queue, MP4 conversion.
EOF

cp -r src pyproject.toml README.md wheels "$PKG/opt/telegram-downloader/"
# Prebuild a wheel: installing from source at runtime fails for non-root
# users (setuptools cannot write egg-info into root-owned /opt).
pip wheel --no-deps -w "$PKG/opt/telegram-downloader/wheels" . 2>&1 | tail -2
cat > "$PKG/opt/telegram-downloader/run.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
shopt -s nullglob
APP=/opt/telegram-downloader
SYS_VENV="$APP/.venv"
# ${HOME:-} + mkdir -p: set -u must not explode when HOME is unset, and
# the per-user venv parent may not exist yet.
USER_VENV="${XDG_DATA_HOME:-${HOME:-/root}/.local/share}/telegram-downloader/.venv"
# Prefer the system venv only if we can write to it (root/admin, or a
# user-writable install); otherwise use a per-user venv since regular
# users cannot write to /opt (a stale root-owned venv must NOT be reused).
if [ -w "$SYS_VENV" ] || { [ ! -e "$SYS_VENV" ] && [ -w "$APP" ]; }; then
  VENV="$SYS_VENV"
else
  VENV="$USER_VENV"
fi
mkdir -p "$(dirname "$VENV")"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
# Install from the prebuilt project wheel (never touches the root-owned
# source tree). pyaes ships sdist-only, so expose our vendored wheel.
export PIP_FIND_LINKS="$APP/wheels"
WHEELS=( "$APP"/wheels/telegram_downloader-*.whl )
if [ "${#WHEELS[@]}" -ne 1 ]; then
  echo "tg-dl: expected exactly 1 project wheel in $APP/wheels, found ${#WHEELS[@]}" >&2
  exit 1
fi
MARKER="$VENV/.tgdl-version"
WHEEL_VER="$(basename "${WHEELS[0]}" | sed -E 's/^telegram_downloader-([0-9][^-]*).*/\1/')"
# Install only when the venv is new or the wheel version changed — not on
# every launch (network, slow, races concurrent runs).
if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER" 2>/dev/null)" != "$WHEEL_VER" ]; then
  if ! "$VENV/bin/pip" install -q -U "${WHEELS[0]}[gui,speed]"; then
    echo "tg-dl: WARNING: gui/speed extras failed; installing base package only." >&2
    "$VENV/bin/pip" install -q -U "${WHEELS[0]}" || {
      echo "tg-dl: ERROR: package install failed." >&2
      exit 1
    }
  fi
  echo "$WHEEL_VER" > "$MARKER"
fi
exec "$VENV/bin/tg-dl" "$@"
EOF
chmod +x "$PKG/opt/telegram-downloader/run.sh"
ln -sf /opt/telegram-downloader/run.sh "$PKG/usr/bin/tg-dl"
cat > "$PKG/usr/share/applications/telegram-downloader.desktop" <<'EOF'
[Desktop Entry]
Name=Telegram Downloader
Exec=tg-dl gui
Icon=telegram-downloader
Terminal=false
Type=Application
Categories=AudioVideo;Network;
EOF
[ -f assets/icon.svg ] && cp assets/icon.svg "$PKG/usr/share/icons/hicolor/scalable/apps/telegram-downloader.svg" || true
chmod 0755 "$PKG/DEBIAN"
mkdir -p dist
DEB="dist/telegram-downloader_${VER}_amd64.deb"
dpkg-deb --build "$PKG" "$DEB"
echo "Built $DEB"
echo "Install: sudo dpkg -i dist/*.deb && tg-dl gui"
