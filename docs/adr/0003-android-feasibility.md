# ADR-0003: Android feasibility of the shared engine

## Status

Proposed — the experiment is incomplete; see "What was and was not verified".

## Context

ADR-0002 splits the project into an engine and clients so that an Android application (Kotlin/Compose, Chaquopy,
Media3) can reuse the Python logic. Whether the engine's native dependencies can run on Android is a separate
question from whether the architecture allows it. This ADR records the evidence so far. It is deliberately not a
merge condition of the split, and the split must not be read as proof that Android works.

Out of scope for now: building our own wheels, a Kotlin HTTP transport, mobile downloads, sync, Compose screens.

## Facts and sources (checked 2026-11-10 unless noted)

| Fact | Source |
|---|---|
| Chaquopy 17.2.0 (2026-10-26) supports Python 3.10–3.15, minSdk 24, AGP 7.3–9.5; Python 3.12+ is 64-bit only | https://chaquo.com/chaquopy/doc/current/changelog.html |
| Since Chaquopy 17.0 pip runs with `--only-binary`: a package without an Android wheel cannot be installed | same changelog |
| "16 KB pages are supported by Chaquopy itself, but many wheels will still fail" | https://chaquo.com/chaquopy/doc/current/ (android / versions notes) |
| `curl-cffi` 0.17.0 publishes Android wheels only for `android_24_arm64_v8a`, cp313/cp314/cp315, and requires `cffi>=2.1.1` | https://pypi.org/pypi/curl-cffi/json |
| `cffi` 2.1.1 has no Android wheel on PyPI; Chaquopy's own index stops at 1.17.1 (cp313) | https://pypi.org/pypi/cffi/json, https://chaquo.com/pypi-13.1/cffi/ |
| `selectolax` 1.0.2 has no Android wheel on PyPI or in Chaquopy's index | https://pypi.org/pypi/selectolax/json, https://chaquo.com/pypi-13.1/ |
| `Brotli` in Chaquopy's index is 1.1.0, up to cp313 | https://chaquo.com/pypi-13.1/brotli/ |
| `yt-dlp` is pure Python with no mandatory dependencies | https://pypi.org/pypi/yt-dlp/json |
| From 2027-02-01 Google Play requires 16 KB page-size support for updates of apps with native code targeting API 35+ | https://developer.android.com/guide/practices/page-sizes |

## What was and was not verified

| Step | Status | Result |
|---|---|---|
| Wheel availability on PyPI for `android_24_arm64_v8a`, CPython 3.14 (`pip download --only-binary=:all: --no-deps`) | **Done** 2026-11-11, from Linux | `curl-cffi`: found (`curl_cffi-0.17.0-cp314-cp314-android_24_arm64_v8a.whl`). `yt-dlp`: found (`py3-none-any`). `cffi`: **none**. `selectolax`: **none**. `brotli`: **none**. Chaquopy's own index was not queried by this command. |
| Bridge contract on the desktop (`tests/bridge`) | **Done** | JSON envelopes, engine/session/operation registries, cancellation by id, listener errors, no token in any payload. This is CPython on Linux, not Chaquopy. |
| Engine imports without terminal or native modules (`tests/architecture`) | **Done** | Importing the engine loads none of `rich`, `InquirerPy`, `platformdirs`, `yaml`, `curl_cffi`, `selectolax`, `yt_dlp`. |
| Gradle build of `apps/android` (APK) | **Not done** | No Android SDK, Gradle or JDK on the machine where the split was written. The Gradle files have never been evaluated; versions other than Chaquopy's are unverified guesses. |
| List of `.so` files in the APK and their LOAD alignment (static 16 KB check) | **Not done** | Needs a built APK. |
| Emulator run (`feasibility.py` report) | **Not done** | Also check `ro.product.cpu.abilist` first: arm64-only wheels need an image that lists `arm64-v8a`; neither its presence nor absence is assumed. |
| Physical ARM64 device run | **Not done** | No device verified yet (ABI of the available phone unknown). |
| TLS fingerprint / Cloudflare behaviour of `curl_cffi` on Android, Wi-Fi with FlareSolverr vs. mobile data without | **Not done** | Needs a device or a suitable emulator. |
| 16 KB page-size runtime | **Not done** | No 16 KB device or image. |
| Media3 playback of a resolved variant (URL + headers + protocol) | **Not done** | |

A successful build, an emulator run, a run on a physical ARM64 device and 16 KB compatibility are separate
confirmations. None of them has been obtained yet.

## Known blockers and options

| Problem | Options | Effect on the architecture |
|---|---|---|
| `curl-cffi` has an Android wheel but its dependency `cffi>=2.1.1` does not | (a) build a `cffi` wheel; (b) Python 3.13 with an older `curl-cffi` and Chaquopy's `cffi` 1.17.1 — unverified; (c) an HTTP transport implemented by the host and injected through `Transport` | (a) none. (b) the engine's `requires-python` would have to drop to 3.13 (PEP 758 `except` syntax is used). (c) none in the engine, but TLS impersonation is lost: do not assume a plain client passes Cloudflare; direct access may end in `AccessBlockedError`. |
| `selectolax` has no Android wheel | (a) build a wheel; (b) a second DOM backend behind `alt_ani_core.dom`, verified with the existing parser fixtures | (b) one new module; parsers unchanged. Not implemented. |
| a native library not aligned for 16 KB | rebuild the wheel with `-Wl,-z,max-page-size=16384` | none |
| `brotli` has no Android wheel | not an engine dependency (the CLI declares it) | none |

What the split already provides for these options: the transport is injected, `curl-cffi` and `selectolax` are
optional extras loaded lazily, the bridge reports `transport_unavailable` instead of failing at import, and a
missing DOM backend is an explicit configuration error.

## Decision

Keep the engine at Python ≥ 3.14 and do not add wheels or a Kotlin transport in the split. Run the remaining
steps from `apps/android/README.md` on Windows 11 with the Android SDK, then on a phone, and update the table above
with each outcome — including failures. Choose between the options only with those results in hand. Full Android
confirmation is a condition for calling a mobile version ready, not for merging the split.

## Consequences

- The mobile client's feasibility is still open: with today's published wheels the engine's default transport and
  DOM backend cannot be installed through Chaquopy.
- The engine can already be exercised on Android with pure-Python pieces (imports, SQLite history, yt-dlp), which
  the spike's report separates from the native ones.
