#!/bin/bash
# Build a .deb for Debian/Ubuntu (Linux target).
# Produces dist/telegram-downloader_<ver>_amd64.deb
# Requires: python3, pip, dpkg-deb. Run on Linux.
set -euo pipefail
cd "$(dirname "$0")/.."
VER="$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])' 2>/dev/null || echo 0.1.0)"
PKG="dist/debroot"
rm -rf dist "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/telegram-downloader" "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps"

cat > "$PKG/DEBIAN/control" <<EOF
Package: telegram-downloader
Version: $VER
Section: video
Priority: optional
Architecture: amd64
Depends: python3, python3-venv, ffmpeg
Maintainer: tgdownloader <noreply@example.com>
Description: Telegram media downloader (GUI + CLI)
 Persistent login, resumable downloads, queue, MP4 conversion.
EOF

cp -r src pyproject.toml README.md "$PKG/opt/telegram-downloader/"
cat > "$PKG/opt/telegram-downloader/run.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
APP=/opt/telegram-downloader
VENV="$APP/.venv"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -U "$APP" "$APP[gui,speed]" || "$VENV/bin/pip" install -q -U "$APP"
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
mkdir -p dist
dpkg-deb --build "$PKG" "dist/telegram-downloader_${VER}_amd64.deb"
echo "Built dist/telegram-downloader_${VER}_amd64.deb"
echo "Install: sudo dpkg -i dist/*.deb && tg-dl gui"
