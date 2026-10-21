// The plugin versions are pinned here and nowhere else.
//
//   AGP 8.13.2   -> needs Gradle 8.13, JDK 17, build-tools 35.0.0, and
//                   supports compileSdk up to 36.1.
//   Kotlin 2.3.21 -> the Compose compiler plugin ships with Kotlin itself,
//                   so the two must be the same version.
//   Chaquopy 17.1 -> supports AGP 7.3 - 9.4 and Python 3.10 - 3.15.
//                   Python 3.12 matches requirements-runtime.lock, so the
//                   .apk and the .deb ship the same telethon.
plugins {
    id("com.android.application") version "8.13.2" apply false
    id("org.jetbrains.kotlin.android") version "2.3.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.3.21" apply false
    id("com.chaquo.python") version "17.1.0" apply false
}
