plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}

// The Python version must match the one the build machine runs: Chaquopy
// needs a matching interpreter to run pip at build time. 3.12 is what
// requirements-runtime.lock pins, so the APK and the .deb ship the same
// telethon.
val chaquopyPythonVersion = "3.12"

// The ABIs this build produces.
//
// The released APK is arm64-v8a only: the other two ABIs were 80 MB of
// dead weight on a device that can only ever run one of them. The emulator
// job needs x86_64, and an arm64-only package cannot be installed on an
// x86_64 emulator -- so the emulator build passes `-PtgdAbis=x86_64`.
//
// (This is the same rule the Flet build follows in scripts/build-apk.sh.)
val targetAbis: List<String> =
    (project.findProperty("tgdAbis") as String?)
        ?.split(",")
        ?.map { it.trim() }
        ?.filter { it.isNotEmpty() }
        ?: listOf("arm64-v8a")

android {
    namespace = "com.tgdownloader.telegram_downloader"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.tgdownloader.telegram_downloader"
        // Chaquopy 17.1 requires API 24 or newer.
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.3.2"

        ndk {
            abiFilters += targetAbis
        }
    }

    buildTypes {
        release {
            // R8 IS worth running here, and the reason it was off was
            // wrong: the Python engine is not in the DEX at all. It is in
            // assets/chaquopy/*.imy -- .py files and compiled extensions --
            // which R8 never sees and cannot break. The DEX holds the
            // Kotlin app and the Compose libraries, and unshrunk those
            // are 21.6 MB of a 33 MB APK.
            //
            // The plain default file, NOT the "optimize" one: the
            // optimized file assumes android.util.Log has no side
            // effects and strips every call to it -- and the log line
            // that says the Python engine is ready is exactly what the
            // CI launch check greps for.
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android.txt"),
                "proguard-rules.pro",
            )
        }
        debug {
            // The debug build is not minified: it is what the emulator
            // job uses to check the app runs, and a shrunk build is
            // much harder to read a stack trace out of.
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

chaquopy {
    defaultConfig {
        version = chaquopyPythonVersion

        // The vendored wheel set, for pyaes.
        //
        // pyaes is published as an SDIST ONLY, and Chaquopy's pip will not
        // build one, so telethon's dependency simply is not on PyPI as a
        // wheel:
        //
        //     ERROR: Could not find a version that satisfies the
        //            requirement pyaes (from telethon) (from versions: none)
        //
        // The vendored set has it -- pyaes is pure Python (py3-none-any),
        // so the same artifact works on Android exactly as it does on
        // Linux. scripts/build-apk.sh solves the same problem for the
        // Flet build with PIP_FIND_LINKS, and the CI job verifies
        // wheels/SHA256SUMS before either build runs.
        val vendoredWheels = rootProject.file("../wheels").absolutePath

        pip {
            // telethon==1.45.0 is the EXACT pin the .deb ships (see
            // pyproject.toml and requirements-runtime.lock): one tag must not
            // produce three artifacts with three different telethons.
            options("--find-links=$vendoredWheels")
            install("telethon==1.45.0")
        }
    }

    sourceSets {
        getByName("main") {
            // The Python app IS the repository's `src/`: one engine, one
            // place to fix it, no copy to keep in sync.
            srcDir("../../src").exclude(
                "**/__pycache__/**",
                "**/*.pyc",
                "**/*.egg-info/**",
            )
        }
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2026.06.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")

    // androidx.core is pinned, and NOT to the newest release. core-ktx
    // 1.19.1 declares minCompileSdk=37 and minAndroidGradlePluginVersion=
    // 9.1.0, so it cannot be used with this project's compileSdk 36 and
    // AGP 8.13.2 -- AGP refuses the build with "4 issues were found
    // when checking AAR metadata". 1.18.0 declares minCompileSdk=36 and
    // minAndroidGradlePluginVersion=8.9.1, which this project satisfies.
    //
    // The constraint was read out of the AARs themselves
    // (META-INF/com/android/build/gradle/aar-metadata.properties), not
    // guessed: 1.19.1 -> 37 / 9.1.0, 1.18.0 -> 36 / 8.9.1,
    // 1.17.0 -> 36 / 8.9.1.
    implementation("androidx.core:core-ktx:1.18.0")
    implementation("androidx.activity:activity-compose:1.12.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.10.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.10.0")

    debugImplementation("androidx.compose.ui:ui-tooling")
}
