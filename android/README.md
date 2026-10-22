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

## The top bar

The bar is the navigation: the brand, the four destinations and the theme
toggle, in the desktop's order. Two things about it are not the desktop's:

**The brand is a fixed slot, outside the scrolling strip.** The
destinations are wider than a phone, so the strip scrolls -- and a
wordmark inside a scrolling strip is a wordmark that scrolls away. The
brand sits in its own slot at the left of the bar and cannot move.

**On a narrow bar the wordmark is the mark alone.** The name is 124dp of
a 420dp phone: 30% of the screen for a label, with five things to reach
beside it. So below 600dp -- the width at which the desktop's whole bar
fits on one line -- the slot keeps only the accent mark, which carries
the name as its accessibility label. At 600dp and above the bar shows
the desktop's brand exactly: mark, name, divider.

When the destination changes, the selected chip is brought back into
view by Compose's own relocation API (`BringIntoViewRequester`), which
scrolls the strip by the minimum that puts the chip on screen.

## The layout on a phone

The GTK app is the reference, and the desktop's window is 1180dp wide. A
phone is 420dp, and the places where that shows are all decided the
same way: **the desktop's arrangement, or the phone's -- never a
half-measure in between.**

**The system bars belong to the app.** `enableEdgeToEdge()` on its own
follows the *system's* night mode, so the navigation bar came up as a
light slab under the dark app, with dark icons on it. The bars are now
painted with the app's own theme -- and repainted when the theme is
flipped -- and the window behind them is the app's page colour.

**A control with no fill is not a control.** A "quiet" button is a
*hover* affordance, and a phone has no hover: "Clear finished" was a
bare label next to two filled buttons. It is filled now, like its
neighbours, and the three queue actions are one group with one
alignment. They do not fit on one line -- the labels are 270dp of text
on a 363dp bar -- so the two that belong together (start the batch or
stop it) share a line and the housekeeping one has a line of its own.

**The dependency rows are the desktop's rows.** The dot, the middle
(which takes the space that is left over) and a trailing box with the
version and the badge -- the same three parts, in the same order, as
`gtk/views/settings.py`. The one thing that had to change is the
alignment: the trailing box sits at the **top** of the row, beside the
name, and not in the middle of it. A phone's description is a paragraph,
and a version centred against a paragraph lands in the middle of it and
the two run into each other.

**A dependency that does not apply here is not shown.** The flet row
belongs to the desktop app -- the .deb ships flet for `tg-dl gui --flet`
and for the web build, and its MISSING badge is correct there. The
Android list marks the rows that do not apply to this platform, and the
screen leaves those out: a red MISSING badge for something the app
cannot use and cannot install is a false alarm.

**The bottom bar says where the downloads go, in words.** The raw
Android path is 80 characters of `/storage/emulated/0/Android/data/
<the package name>/`, so the bar shows the last two segments with an
ellipsis in front -- the same rule as the desktop's `~/Downloads/
Telegram`, which shows two segments after the `~`.

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
