#!/bin/bash
# Launch the .apk in a real Android runtime and prove it does not die.
#
# Run inside the android-emulator-runner's emulator. The APK is a Flet app:
# the Dart/Flutter side is the shell and the Python side runs under
# Chaquopy. A Python that cannot even import its own dependencies shows up
# here as a crash on launch and, on the Flutter side, as a red error screen
# instead of a window.
#
# This is the check the whole test suite could not do: the v1.3.2 APK was
# published with NO flet in its Python runtime, so `import flet` died on the
# first line of the app and every user saw a red screen.
set -uo pipefail

APK="$(ls "$GITHUB_WORKSPACE"/dist/*.apk 2>/dev/null | head -1)"
if [ -z "$APK" ]; then
  echo "no .apk in dist/"
  exit 1
fi
echo "==> installing $(basename "$APK")"
adb install -r -g "$APK" || exit 1

PKG="com.tgdownloader.telegram_downloader"

adb logcat -c
adb shell am force-stop "$PKG" || true
echo "==> launching"
adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || \
  adb shell am start -n "$PKG/.MainActivity" || true

# Give the Python runtime time to start, import everything and build the
# first frame. A crash lands here, in the first few seconds.
sleep 25

echo "==> logcat (app + python + flutter)"
adb logcat -d -v brief 2>/dev/null \
  | grep -iE "chaquopy|python|flutter|telegram_downloader|AndroidRuntime|FATAL" \
  | tail -60 || true

echo "==> process state"
if adb shell pidof "$PKG" >/dev/null 2>&1 && [ -n "$(adb shell pidof "$PKG" 2>/dev/null)" ]; then
  echo "the app process is ALIVE"
else
  echo "::error::the app process is not running -- it died on launch"
  adb logcat -d -v brief 2>/dev/null | tail -80
  exit 1
fi

# A Python traceback in the app's own process is the failure mode that
# matters here: the Flutter shell survives it and shows a red screen.
TRACEBACK="$(adb logcat -d -v brief 2>/dev/null \
  | grep -iE "Traceback \(most recent call last\)|ModuleNotFoundError|ImportError" \
  || true)"
if [ -n "$TRACEBACK" ]; then
  echo "::error::the app's Python side raised on launch:"
  echo "$TRACEBACK"
  exit 1
fi

echo "==> taking a screenshot"
adb exec-out screencap -p > "$GITHUB_WORKSPACE/dist/apk-launch.png" 2>/dev/null || true
echo "OK: the app launched and stayed up"
