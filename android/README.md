# The native Android app

Kotlin + Jetpack Compose over the **existing** Python engine. Chaquopy runs
the engine in-process: there is no socket, no HTTP server and no Flutter.
Kotlin calls `telegram_downloader.bridge` directly.

This is the first slice: the top bar and the Add screen, plus the other
three destinations in the same pattern.

## Layout

```
android/
  settings.gradle.kts       plugin + repository setup
  build.gradle.kts          the pinned plugin versions
  gradle.properties
  app/
    build.gradle.kts        the Chaquopy plugin, the ABIs, the dependencies
    src/main/
      AndroidManifest.xml
      res/                   the launcher mark, the theme, the strings
      kotlin/com/tgdownloader/telegram_downloader/
        MainActivity.kt      the Compose host, the file pickers
        Bridge.kt            Kotlin -> Python
        Theme.kt             the design tokens
        TgViewModel.kt       the screen state, every call into the engine
        ui/                  the four screens and the shared primitives
```

The Python side is NOT copied into this tree: `app/build.gradle.kts` points
Chaquopy's source set at the repository's `src/`, so the APK ships exactly
the engine the desktop app ships, and a fix in `src/telegram_downloader/`
reaches the phone without a second copy to keep in sync.

## Building

There is no Gradle wrapper in this directory. A wrapper is a binary jar
that cannot be reviewed in a diff, and the CI job pins the exact Gradle
version itself (`gradle/actions/setup-gradle` with `gradle-version:
"8.13"`). To build by hand:

```sh
# Python 3.12 on the PATH (Chaquopy runs pip at build time and the
# interpreter's major.minor must match the app's), JDK 17, and the Android
# SDK with platform 36 and build-tools 35.0.0.
cd android
gradle :app:assembleDebug -PtgdAbis=x86_64     # the emulator's APK
gradle :app:assembleRelease -PtgdAbis=arm64-v8a # the APK that would ship
```

The versions are pinned in `build.gradle.kts`: AGP 8.13.2, Kotlin 2.3.21,
Chaquopy 17.1.0, Python 3.12.

## The ABIs

The shipped APK is **arm64-v8a only** -- the other two ABIs were 80 MB of
dead weight on a device that can only ever run one of them. The emulator
job needs x86_64, and an arm64-only package cannot be installed on an
x86_64 emulator, so the emulator build passes `-PtgdAbis=x86_64`. This is
the same split `scripts/build-apk.sh` makes for the Flet APK.

## The Android-specific decisions

**The config directory.** `config.py` resolves `~/.config/telegram-downloader/`
and then refuses to start if that directory is a symlink, is owned by
somebody else, or is group/world writable. `bridge.init()` therefore points
`XDG_CONFIG_HOME` at `<files>/xdg`, created 0700 by the app itself, before
`config.py` is imported for the first time -- import order is the whole
trick, because `config.py` computes `CONFIG_DIR` when it is imported.
Internal storage is used on purpose: it is app-private, it survives
reboots, and the OS does not clear it.

**The download folder.** `~/Downloads/Telegram` does not exist on Android.
The default is the app's own folder on the shared external volume
(`getExternalFilesDir(DIRECTORY_DOWNLOADS)`), which is a real path the
engine can write to on every Android version **without a runtime
permission**, and which survives an app restart. The "Change" button opens
the system folder picker; the picked tree is resolved to a filesystem path
and probed for writability, because the engine writes through the
filesystem and Android's scoped storage would otherwise let the app accept
a folder it cannot write. When the probe fails the app says so and keeps
the folder it had.

**The file picker.** The `.txt` batch file comes back as a `content://` URI,
not a path, so the **bytes** are read through the content resolver -- the
same approach the desktop app takes -- with the size cap checked while
reading.

**No ffmpeg.** Android has no ffmpeg and the app cannot install one, so
the Fast and Max (MP4) modes are OFF in the Add screen, the notice says so
plainly, and the Settings screen shows the ffmpeg row as MISSING with the
reason. Nothing fails at the end of a long download.

**No storage permission.** None is requested and none is needed: the
download folder is the app's own, and the one file the user picks is read
through the picker.

## The threading

The engine is asyncio (telethon requires it) and the Android UI thread must
never block. `bridge.py` mirrors `gtk/engine.py`: one event loop in a
background thread, and a call that needs the loop hops to that thread and
waits there. On the Kotlin side every call is a `suspend` function that
hops to `Dispatchers.IO` first, so the UI thread is never the one waiting.
A download that takes minutes is *submitted*, not awaited.

## What CI proves

The `apk-native` job builds the APK with Gradle; `apk-native-emulator`
installs the x86_64 one on an emulator and asserts that the engine started
in-process, that the app is the foreground activity, and that the Compose
UI is on screen (`.github/workflows/apk-smoke.sh`, with
`TG_NATIVE_UI_ASSERT=1`).

The Flet `apk` job is untouched and still publishes the released APK. When
the native app becomes the shipped artifact, that job is removed
deliberately in the same change -- the two are never left half-wired.
