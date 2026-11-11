plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.chaquopy)
}

android {
    namespace = "pl.altani.spike"
    compileSdk = 36

    defaultConfig {
        applicationId = "pl.altani.spike"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.0.1-spike"

        ndk {
            // arm64-v8a is the only ABI curl-cffi publishes Android wheels for; x86_64 serves pure-Python emulator runs.
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

chaquopy {
    defaultConfig {
        // 3.14 matches the engine's requires-python; curl-cffi ships cp313/cp314/cp315 Android wheels only.
        version = "3.14"

        pip {
            install("yt-dlp")
            // Enable one at a time and record the outcome in docs/adr/0003-android-feasibility.md:
            //   curl-cffi needs cffi>=2.1.1, which had no Android wheel when this was written.
            //   selectolax had no Android wheel at all.
            // install("curl-cffi")
            // install("selectolax")
        }
    }

    sourceSets {
        getByName("main") {
            // The engine is consumed as source; the alternative is installing its py3-none-any wheel through pip.
            srcDir("../../../packages/core/src")
        }
    }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.material3)
    implementation(libs.compose.ui)
    implementation(libs.activity.compose)
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.exoplayer.hls)
    implementation(libs.media3.exoplayer.dash)
    implementation(libs.media3.datasource)
    implementation(libs.media3.ui)
}
