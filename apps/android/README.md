# Android app

The app loads the shared engine (`packages/core`) and the JSON bridge (`app/src/main/python/alt_ani_bridge`) through
Chaquopy and plays media through Media3. It has two tabs: [Catalog](#catalog-tab) (search → episode → playback on real
Shinden) and Tech, the original feasibility screen that prints what works on the device. The feasibility results and
the D1 decision (path A) are in [ADR-0003](../../docs/adr/0003-android-feasibility.md). Nothing in CI builds or tests
it.

The bridge itself is covered on the desktop by `tests/bridge/` (no Android toolchain needed); `just test` runs only
the Python suite.

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

## JVM unit tests

The Kotlin unit tests (`app/src/test`) need neither a device nor Python:

```powershell
.\gradlew :app:testDeviceDebugUnitTest
```

## Tests on a device or emulator

Pass `-s <serial>` to every `adb` command when more than one device is attached.

Controlled suite (bridge, Media3 playback, consent gate, stale starts, resume order) against `tools/android/controlled_server.py`:

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

## Catalog tab

The app opens on **Catalog**: search → series → episodes → sources, on the real Shinden through the process's bridge
(never the controlled server), with playback by the same `PlaybackController` as the controlled flow. **Tech** keeps the
technical buttons and the controlled playback; starting one flow closes the other's player. The engine is created
without FlareSolverr, so a Cloudflare block shows as an access error.

The device's network changes reach the engine as `network_set`; a failed or unanswered call (15 s) is sent again after
1 s, doubling up to 30 s, until the engine takes it or the device moves on. The catalogue player does not wait for that:
mobile data or offline on the device closes its transfer gate at once, and the gate opens only when the device and the
bridge both allow it (Wi-Fi, or mobile data with the per-episode consent), so a late `network_set` never lets a stream
load over LTE. A VPN counts by the transport it reports and, when it reports none, as mobile data unless the system
calls it unmetered.

Search results come by **Best match**, computed on the device: exact title, then titles starting with the query, whole
words, substring, all words anywhere; ties go TV, ONA, OVA, film, special, then by title. **Newest first** reads the
air dates of all hits with one `metadata_many` call (once per search; Cancel or opening a series drops it) and lists
titles without a date last, as the CLI's date sort does. A failed read falls back to Best match.

An episode opens with `open_episode` and lists its players; **Auto** lets the bridge choose by the preferences, a
listed player is played even outside them. Both reuse that episode session for `playback_prepare`. A failed player
leaves the list on screen for another pick; a session the bridge closed (e.g. after a network change) is reopened once,
and an expired one offers **Reload sources** instead of Retry. Sessions neither listed nor playing are closed, including
one whose `open_episode` reply came back only after the user had moved on (the runtime's late results).

The status line separates three things: the bridge answered (lists appear), a stream was resolved (`stream: online
<variant>`), and Media3 really played (`media3: confirmed` — at least 3 s of position progress in this attempt plus a
rendered first frame for video, audio output reported by Media3 for audio only). `PLAYING` alone is not a
confirmation.

Turning the device sideways while the catalogue plays shows the player alone on the screen, without system bars; an
error or the mobile-data consent keeps the regular layout so the message stays visible. The player's fullscreen button
turns the screen (Back leaves fullscreen); once the device is held that way, auto-rotate takes over again, and with
auto-rotate off the button's orientation stays.

### Watch history and Resume

The engine's SQLite is the only record; the tab keeps just the last reads of it. **Recently watched** (`watch_recent`,
20 series by their latest opening) is shown while no search is on screen; **Clear** or Back from the results returns to
it. A series opens the same way from the history and from a search hit (its `SeriesRef`), listing its episodes with
`episodes` and reading `watch_series` beside it: **Last opened** names the episode with its saved position, the list
marks watched episodes (`✓`) and known positions and scrolls once to the last opened one. A last episode missing from
the current Shinden listing is named but cannot be resumed, since the watch state carries no episode URL.

**Resume** and **Start over** open that episode's sources; nothing plays until a source is picked. The sources screen
shows where playback will start whenever a position is known and lets the user switch before picking. `playback_prepare`
always reports the saved position; only a pick made with **Start over** begins at 0, and only that one pick (a Retry
after it has played resumes). A watched episode saved at its end offers Start over instead of Resume, but is never
started over without that choice. Positions and the watched flag follow the engine's rules unchanged.

The history is read again when its screen is shown (also after rotation or Home), after a non-periodic position or an
end the engine accepted for that series (`accepted: true`, matched by the viewing's own `series_id`, so a late reply of
an earlier viewing never lands on another series), and once more when a write lands while a read is in flight. History
reads run beside the catalogue's calls: they never cancel a search or a listing, and their failure has its own Retry.

A new start of the player first waits for the previous viewing's last position and `playback_close` to be answered
(at most 2 s), so its `playback_begin` does not supersede that viewing before the position is kept and
`playback_prepare` reads it. If the bridge does not answer in time the start goes on and that last write may be
refused, losing at most the seconds since the previous periodic write (10 s).

Manual check on a `device` build: search a known title (e.g. Frieren), open its episodes, pick one and wait for
`media3: confirmed`; pause and resume, rotate (playback goes on), Home and back (stays paused), pick another episode,
then airplane mode and a new search for the network message and Retry. History on Wi-Fi (no SIM or FlareSolverr
needed): Back to the catalogue lists the series under Recently watched with its position; open it from there (no
search), **Resume** shows "Resume at m:ss" on the sources screen and plays from about there; switching the source
while playing continues from the current position; **Start over** plays from 0:00; after the episode ends it is marked
`✓` and the card offers only Start over; after closing the app from Recents the history is still there and nothing
plays. Evidence without URLs or tokens:

```powershell
adb logcat -s AltAniCatalog:I AltAniPlayback:I AltAniNetwork:I
```

## MIUI

Xiaomi phones without a SIM refuse `adb install` (`INSTALL_FAILED_USER_RESTRICTED`): push the APK to
`/sdcard/Download` and install it from the phone. They also deny key injection and Activity starts from the test
process, so `PlaybackLifecycleTest` and `ConsentUiTest` cannot run there; check the lifecycle by hand on the
technical screen instead (rotation, Home, screen lock).
