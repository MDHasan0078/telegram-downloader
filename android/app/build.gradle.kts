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
            // The Python engine is the product; shrinking it buys little and
            // a reflection-heavy library (telethon) is exactly what R8 gets
            // wrong. The APK is not minified.
            isMinifyEnabled = false
        }
        debug {
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

    implementation("androidx.core:core-ktx:1.19.1")
    implementation("androidx.activity:activity-compose:1.12.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.10.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.10.0")

    debugImplementation("androidx.compose.ui:ui-tooling")
}
