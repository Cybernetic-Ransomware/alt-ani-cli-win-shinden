# Android feasibility spike

Not a product: one technical screen that loads the shared engine (`packages/core`) and the JSON bridge
(`app/src/main/python/alt_ani_bridge`) through Chaquopy, plays media through Media3 and prints what works on the
device. Results and the D1 decision (path A) are in [ADR-0003](../../docs/adr/0003-android-feasibility.md).
Nothing in CI builds it.

The bridge itself is covered on the desktop by `tests/bridge/` (no Android toolchain needed).

## Flavors

| Flavor | ABI | Python requirements | Talks to |
|---|---|---|---|
| `device` | arm64-v8a | `app/requirements-device.txt`: `curl-cffi` 0.17.0 on our `cffi` 2.1.1 wheel from `wheels/` | real hosts and the controlled server |
| `emulator` | x86_64 | `app/requirements-emulator.txt`: no `curl-cffi` (no x86_64 wheel) | the controlled server only; never released |

## Building

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
cd apps/android
.\gradlew :app:assembleDeviceDebug :app:assembleDeviceDebugAndroidTest
```

The APKs to install are in `app/build/outputs/apk/device/debug/` and `app/build/outputs/apk/androidTest/device/debug/`,
never in `app/build/intermediates/`. `tools/android/bootstrap-wrapper.ps1` regenerates the Gradle wrapper without an
installed `gradle`.

Static 16 KB check of every native library, including the ones nested in Chaquopy's assets:

```powershell
uv run python tools/android/check_native_libs.py apps/android/app/build/outputs/apk/device/debug/app-device-debug.apk
```

## Tests on a device or emulator

Pass `-s <serial>` to every `adb` command when more than one device is attached.

Controlled suite (bridge, Media3 playback, consent gate, stale starts) against `tools/android/controlled_server.py`:

```powershell
uv run python tools/android/controlled_server.py --port 8765   # separate terminal
adb reverse tcp:8765 tcp:8765
adb shell am instrument -w pl.altani.spike.test/androidx.test.runner.AndroidJUnitRunner
```

Without the server the controlled tests are skipped; `-e controlledServer required` makes them fail instead. The T9
trial is opt-in, see [T9](../../docs/android/t9-transfer-gate-trial.md).

Live Shinden on a `device` build (opt-in, real network):

```powershell
adb logcat -c
adb shell am instrument -w -e shindenLive true -e class pl.altani.spike.live.ShindenLiveTest `
    pl.altani.spike.test/androidx.test.runner.AndroidJUnitRunner
adb logcat -d -s ShindenLive:I
```

Optional arguments: `-e access flaresolverr -e flaresolverr http://<host>:8191`, `-e query <title>`,
`-e episodeIndex <n>`, `-e playSeconds <s>`. The log carries codes, counts and timings only; each run uses its own
runtime directory, deleted afterwards, because it may hold a `cf_clearance` cookie. The playback check fails on a
seek, so do not touch the player while it runs.

## MIUI

Xiaomi phones without a SIM refuse `adb install` (`INSTALL_FAILED_USER_RESTRICTED`): push the APK to
`/sdcard/Download` and install it from the phone. They also deny key injection and Activity starts from the test
process, so `PlaybackLifecycleTest` and `ConsentUiTest` cannot run there; check the lifecycle by hand on the
technical screen instead (rotation, Home, screen lock).
