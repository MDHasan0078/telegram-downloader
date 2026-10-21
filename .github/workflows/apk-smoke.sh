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

if [ ! -s "$SHOT" ]; then
  echo "::error::no screenshot: the app did not render anything"
  exit 1
fi

if [ "${TG_NATIVE_UI_ASSERT:-0}" = "1" ]; then
  # The NATIVE app (Kotlin/Compose over the same Python engine).
  #
  # The colour heuristic below is Flet's: it looks for the flat red error
  # panel a Flutter app shows when its Python side dies. A Compose app has
  # no such panel, and its dark surface has few distinct colours, so the
  # heuristic would fail it for the wrong reason.
  #
  # What is checked instead is what actually matters here: the engine
  # started IN-PROCESS (Chaquopy is up, and the engine accepted the config
  # directory -- the Android-specific failure this job exists to catch),
  # the app is the foreground activity, and the Compose tree is on screen.
  echo "==> the engine started in-process?"
  READY="$(adb logcat -d -v brief 2>/dev/null | grep -i "TgBridge" || true)"
  if [ -z "$READY" ]; then
    echo "::error::the app never reported a ready engine (Chaquopy or the config dir failed)"
    adb logcat -d -v brief 2>/dev/null | tail -80
    exit 1
  fi
  echo "$READY" | tail -5

  echo "==> the app is the foreground activity?"
  RESUMED="$(adb shell dumpsys activity activities 2>/dev/null \
    | grep -iE "mResumedActivity|topResumedActivity" | head -1 || true)"
  case "$RESUMED" in
    *"$PKG"*) echo "  $RESUMED" ;;
    *)
      echo "::error::the app is not in the foreground: ${RESUMED:-<nothing resumed>}"
      exit 1
      ;;
  esac

  echo "==> the UI came up?"
  UI="$(adb shell uiautomator dump /sdcard/tg-ui.xml >/dev/null 2>&1 \
    && adb shell cat /sdcard/tg-ui.xml 2>/dev/null || true)"
  if [ -z "$UI" ]; then
    echo "::error::the accessibility tree is empty -- the UI never came up"
    exit 1
  fi

  # The whole tree, saved as an artifact. When this job fails on an
  # assertion, the question is always "what WAS on screen?", and the
  # answer has to be in the log rather than on somebody's laptop.
  printf '%s' "$UI" > "$GITHUB_WORKSPACE/dist/tg-ui.xml" 2>/dev/null || true

  # And a readable summary of it: every string the app put on screen.
  # uiautomator's XML is one enormous line, so the interesting part --
  # the text -- is invisible in the raw dump.
  NODES="$(printf '%s' "$UI" | grep -o '<node ' | wc -l | tr -d ' ')"
  echo "  the accessibility tree has $NODES node(s)"
  printf '%s' "$UI" | grep -o 'text="[^"]*"' | sed 's/^text="//; s/"$//' \
    | grep -v '^$' | sort -u | sed 's/^/  text: /' | head -40
  printf '%s' "$UI" | grep -o 'content-desc="[^"]*"' | sed 's/^content-desc="//; s/"$//' \
    | grep -v '^$' | sort -u | sed 's/^/  desc: /' | head -20

  # The app's own strings. The destinations are labels the app renders,
  # so their presence is proof that the Compose tree is not just a
  # background.
  #
  # A tree with a handful of nodes is a blank screen, which is the
  # failure this job exists to catch, and it is a different failure from
  # a missing string. The count is checked first so a blank screen is
  # reported as a blank screen.
  if [ "$NODES" -lt 8 ]; then
    echo "::error::the accessibility tree has only $NODES node(s) -- the UI is blank"
    exit 1
  fi
  for NEEDLE in "Add" "Ongoing"; do
    case "$UI" in
      *"$NEEDLE"*) echo "  the UI shows: $NEEDLE" ;;
      *)
        echo "::error::the UI does not show \"$NEEDLE\""
        exit 1
        ;;
    esac
  done
  # Reported, not required: the wordmark is a text label in the bar, but
  # whether it reaches the accessibility tree is a Compose detail, and the
  # destinations above are the assertion that matters.
  case "$UI" in
    *"Telegram Downloader"*) echo "  the UI shows: the wordmark" ;;
    *) echo "  (the wordmark is not in the accessibility tree -- noted, not required)" ;;
  esac

  # Every screen, not just the one the app opens on.
  #
  # The bar is on every screen, so a bar that is wrong is wrong on all
  # of them -- and the launch check looked at one. This walks the
  # destinations, taps each one by its own label, captures it, and
  # asserts that each screen shows its OWN content: a destination that
  # still shows the previous screen is a broken app that no screenshot
  # of one screen would ever reveal.
  echo "==> every screen"
  python3 "$GITHUB_WORKSPACE/scripts/android-screens.py" \
    --out "$GITHUB_WORKSPACE/dist" || exit 1
else
  # The symptom that matters: the app showing a RED ERROR SCREEN instead of
  # its UI. In Flet that is a flat red panel, so it is caught two ways -- it
  # has very few distinct colours (it is one red fill with a little text on
  # it), and its red channel dominates the whole frame.
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
fi

echo "OK: the app launched, stayed up, and rendered its UI"
