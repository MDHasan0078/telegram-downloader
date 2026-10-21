// The Android native app: Kotlin + Jetpack Compose over the existing Python
// engine, with Chaquopy as the bridge.
//
// The Python source is NOT copied into this tree: `app/build.gradle.kts`
// points Chaquopy's source set at the repository's `src/`, so the APK ships
// exactly the engine the desktop app ships.
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "telegram-downloader-android"
include(":app")
