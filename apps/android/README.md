# Android feasibility spike

Not a product: one screen that loads the shared engine (`packages/core`) and the JSON bridge
(`app/src/main/python/alt_ani_bridge`) through Chaquopy and prints a report of what works on the device.
It exists to answer the open questions in [ADR-0003](../../docs/adr/0003-android-feasibility.md).
It is **not** part of the merge gate of the engine/CLI split and nothing in CI builds it.

The bridge itself is covered on the desktop by `tests/bridge/` (no Android toolchain needed).

## Status

Nothing in this directory has been built yet. The Gradle files were written without an Android SDK at hand;
only the Chaquopy version was checked against its changelog. Expect to adjust plugin and library versions in
`gradle/libs.versions.toml` on the first build.

## Running the experiment (Windows 11 + Android SDK)

Record every step's outcome in ADR-0003, including the ones you could not run.

1. **Wheel availability (no build needed)** — for each native dependency:
   ```powershell
   py -3.14 -m pip download --only-binary=:all: --no-deps --platform android_24_arm64_v8a --python-version 3.14 -d wheels curl-cffi
   py -3.14 -m pip download --only-binary=:all: --no-deps --platform android_24_arm64_v8a --python-version 3.14 -d wheels cffi
   py -3.14 -m pip download --only-binary=:all: --no-deps --platform android_24_arm64_v8a --python-version 3.14 -d wheels selectolax
   ```
   Chaquopy 17 installs with `--only-binary`, so a package without an Android wheel cannot be installed at all.
2. **Build** — `gradle wrapper` once, then `.\gradlew :app:assembleDebug`. Enable the commented `install(...)`
   lines in `app/build.gradle.kts` one at a time.
3. **Native libraries and 16 KB page size (static)** — list the `.so` files in the APK and check their LOAD
   segment alignment:
   ```powershell
   & "$env:ANDROID_HOME\build-tools\<version>\zipalign.exe" -c -P 16 -v 4 app\build\outputs\apk\debug\app-debug.apk
   & "$env:ANDROID_HOME\ndk\<version>\toolchains\llvm\prebuilt\windows-x86_64\bin\llvm-readelf.exe" -l <extracted .so>
   ```
   Alignment of `0x4000` or more on every LOAD segment is required for 16 KB devices.
4. **Emulator** — first check what the image can run, do not assume either way:
   ```powershell
   adb shell getprop ro.product.cpu.abilist
   ```
   An x86_64 image that does not list `arm64-v8a` cannot load arm64-only wheels (curl-cffi); it can still run
   the pure-Python checks. Note whether a result came from native x86_64 code or ARM translation.
5. **Physical ARM64 device** (when available) — same report, plus a real request to shinden.pl on Wi-Fi with
   FlareSolverr and on mobile data without it.
6. **16 KB runtime** — only on a 16 KB device or emulator image; otherwise it stays unverified.

A successful build, an emulator run, a run on a physical ARM64 device and 16 KB compatibility are four
separate confirmations; none implies another.
