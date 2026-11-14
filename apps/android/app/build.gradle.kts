plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.chaquopy)
}

val pythonVersion = "3.14"

// Per-machine interpreter for Chaquopy's pip; unset means Chaquopy's own lookup (py -3.14 / python3.14)
val buildPythonCommand: List<String>? =
    ((findProperty("altani.buildPython") as String?) ?: System.getenv("ALTANI_BUILD_PYTHON"))
        ?.split(" ")
        ?.filter { it.isNotBlank() }
        ?.takeIf { it.isNotEmpty() }

android {
    namespace = "pl.altani.spike"
    compileSdk = 37

    defaultConfig {
        applicationId = "pl.altani.spike"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.0.1-spike"
    }

    flavorDimensions += "target"
    productFlavors {
        create("device") {
            dimension = "target"
            // the only ABI curl-cffi publishes Android wheels for
            ndk { abiFilters += "arm64-v8a" }
        }
        create("emulator") {
            dimension = "target"
            ndk { abiFilters += "x86_64" }
        }
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

androidComponents {
    // the emulator flavor has no HTTP transport for real hosts and is never shipped
    beforeVariants(selector().withFlavor("target" to "emulator").withBuildType("release")) { it.enable = false }
}

chaquopy {
    defaultConfig {
        // matches the engine's requires-python; never lowered (PEP 758 syntax in the engine)
        version = pythonVersion
        buildPythonCommand?.let { buildPython(*it.toTypedArray()) }
    }
    productFlavors {
        getByName("device") {
            pip {
                options("--find-links", rootProject.file("wheels").absolutePath)
                install("-r", "requirements-device.txt")
            }
        }
        getByName("emulator") {
            pip {
                install("-r", "requirements-emulator.txt")
            }
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("../../../packages/core/src")
        }
    }
}

val checkBuildPython = tasks.register("checkBuildPython") {
    description = "Fails early unless the interpreter Chaquopy's pip runs on is Python $pythonVersion."
    val isWindows = System.getProperty("os.name").startsWith("Windows")
    val command = buildPythonCommand ?: if (isWindows) listOf("py", "-$pythonVersion") else listOf("python$pythonVersion")
    val expected = pythonVersion
    doLast {
        val probe = "import sys; print('%d.%d' % sys.version_info[:2])"
        val found = try {
            val process = ProcessBuilder(command + listOf("-c", probe)).redirectErrorStream(true).start()
            val output = process.inputStream.bufferedReader().readText().trim()
            if (process.waitFor() == 0) output.lines().last() else "failed: $output"
        } catch (e: java.io.IOException) {
            "not found (${e.message})"
        }
        if (found != expected) {
            throw GradleException(
                "Chaquopy needs Python $expected on the build machine; `${command.joinToString(" ")}` gave: $found. " +
                    "Set altani.buildPython (Gradle property) or ALTANI_BUILD_PYTHON to a Python $expected command."
            )
        }
    }
}

tasks.configureEach {
    // every Chaquopy task that may run the build interpreter (lookup, pip, proxies, compileall)
    if (name.contains("Python") && name != checkBuildPython.name) {
        dependsOn(checkBuildPython)
    }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.material3)
    implementation(libs.compose.ui)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.lifecycle.process)
    implementation(libs.coroutines.android)
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.exoplayer.hls)
    implementation(libs.media3.exoplayer.dash)
    implementation(libs.media3.datasource)
    implementation(libs.media3.ui)
}
