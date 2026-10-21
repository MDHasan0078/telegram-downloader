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
  ls -R "$GITHUB_WORKSPACE"/dist 2>/dev/null || true
  exit 1
fi
echo "==> installing $(basename "$APK")"
adb install -r -g "$APK" || exit 1

# The package name is NOT hardcoded. Flet derives it from the Python
# module -- telegram_downloader.gui becomes
# com.tgdownloader.telegram_downloadergui -- and a hardcoded name that
# does not match is exactly the kind of thing that breaks when a module is
# renamed. Ask the package manager instead.
PKG="$(adb shell pm list packages 2>/dev/null \
        | tr -d '\r' | sed -n 's/^package:\(.*telegram[._]downloader.*\)$/\1/p' \
        | head -1)"
if [ -z "$PKG" ]; then
  echo "::error::the app did not register a package after install"
  adb shell pm list packages 2>/dev/null | grep -i telegram || true
  exit 1
fi
echo "==> package: $PKG"

adb logcat -c
adb shell am force-stop "$PKG" 2>/dev/null || true
echo "==> launching"
# `monkey` with the LAUNCHER category resolves whatever the real launch
# activity is, so this does not depend on the activity's class name.
adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 2>&1 | tail -3

# Give the Python runtime time to start, import everything and build the
# first frame. A crash lands here, in the first few seconds.
for _ in $(seq 1 18); do
  sleep 2
  if [ -n "$(adb shell pidof "$PKG" 2>/dev/null | tr -d '\r')" ]; then
    break
  fi
done
sleep 6

echo "==> logcat (app + python + flutter)"
adb logcat -d -v brief 2>/dev/null \
  | grep -iE "chaquopy|python|flutter|telegram_downloader|AndroidRuntime|FATAL" \
  | tail -50 || true

echo "==> process state"
PID="$(adb shell pidof "$PKG" 2>/dev/null | tr -d '\r')"
if [ -n "$PID" ]; then
  echo "the app process is ALIVE (pid $PID)"
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
SHOT="$GITHUB_WORKSPACE/dist/apk-launch.png"
adb exec-out screencap -p > "$SHOT" 2>/dev/null || true
ls -la "$SHOT" 2>/dev/null || true

# The symptom that matters: the app showing a RED ERROR SCREEN instead of
# its UI. In Flet that is a flat red panel, so it is caught two ways -- it
# has very few distinct colours (it is one red fill with a little text on
# it), and its red channel dominates the whole frame.
if [ -s "$SHOT" ]; then
  COLOURS="$(identify -format '%k' "$SHOT" 2>/dev/null || echo 0)"
  RED="$(convert "$SHOT" -format '%[fx:mean.r]' info: 2>/dev/null || echo 0)"
  echo "the screen has $COLOURS distinct colours, mean red $RED"
  if [ "$COLOURS" -lt 50 ]; then
    echo "::error::the app shows a flat screen ($COLOURS colours) -- this is"
    echo "::error::what the red error panel looks like; the UI never rendered"
    exit 1
  fi
  # A red error panel is mostly red. The app is dark and mostly not.
  if awk "BEGIN{exit !($RED > 0.35)}"; then
    echo "::error::the screen is mostly red (mean red $RED) -- that is the"
    echo "::error::Flet's error panel, not the app"
    exit 1
  fi
else
  echo "::error::no screenshot: the app did not render anything"
  exit 1
fi

echo "OK: the app launched, stayed up, and rendered its UI"
