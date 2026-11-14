# T9 — holding Media3 transfers without consent

Trial behind the K3 decision in [ADR-0006](../adr/0006-mobile-client-contract.md). Two runs on 2026-11-14 with the same
results. Environment: emulator `Medium_Phone_API_37.0` (x86_64), Media3 1.11.1, `emulatorDebug`, controlled server on
`127.0.0.1:8765` through `adb reverse`. Not run on a physical device yet.

## Method

`TransferGateTrialTest` runs every candidate through the cases below. `TransferProbe` wraps the HTTP data source
twice: the outer wrapper numbers each `open()` and stamps the gate state when the open starts, and the inner wrapper,
below the candidate's hold, stamps it again just before the request is sent. Both stamps are taken under the gate's
own lock. The number goes out as `X-Probe-Seq`, and the server logs it with the client port, the status, and whether
the response was finished or aborted.

- **violation**: the open started while the gate was closed and its request was sent in the same closed period. This
  must be zero.
- **raced**: the open started while the gate was open, and the request was sent after the close. This is the limit of
  the guarantee, not a failure.

Candidates:

- K1: `GatedLoadControl` refuses to load while the gate is closed.
- K2: `ThrowingGateDataSource` fails `open()`; `GateRetryPolicy` polls every 500 ms with no retry limit.
- K3: `GatedDataSource` makes `open()` wait for the gate.
- K1_K3: K1 and K3 together.

Cases:

| case | what happens |
|---|---|
| 1_idle | the gate closes while playing; the buffer runs out; the gate opens |
| 2a / 2b | the probe stops one `open()` just before or just after the gate check; the gate closes at that point |
| 3_redirect | a segment answers 302 after a delay; the gate closes during the delay |
| 4_retry | a segment fails with 503 twice; the gate closes before the retry |
| 5a_live_refresh | an EVENT playlist that grows; the gate closes between playlist refreshes |
| 5b_closed_start | `prepare()` with the gate already closed |
| 6_in_flight | a throttled segment is being transferred when the gate closes |
| 8_cancel_release | a seek, then `release()`, while an `open()` waits |
| 9_long_hold | the no-progress detector cut to 3 s, without the controller's pause |

## Results

`v` = violations and `r` = raced requests, for run 1 and then run 2. `resume` is the time from opening the gate until
playback runs again (−1 means it never did).

| case | K1 | K2 | K3 | K1_K3 |
|---|---|---|---|---|
| 1_idle | `FAILED_RUNTIME_CHECK`, no resume | v0 r0, 1.2 / 1.1 s | v0 r0, 1.0 / 1.1 s | `FAILED_RUNTIME_CHECK`, no resume |
| 2a | **v1** / **v1** | v0 r0 | v0 r0 | v0 r0 |
| 2b | r1 / r1 | r1 / r1 | r1 / r1 | r1 / r1 |
| 3_redirect | v0 r0, target after the close | same | same | same |
| 4_retry | **v1** / **v1** | v0 r0, 7 failed opens while closed | v0 r0, one waiting open | v0 r0 |
| 5a_live_refresh | **v3** / **v3** | v0 r0, 0.3 s | v0 r0, 0.1 s | v0 r0, 0.1 s |
| 5b_closed_start | **v2** / **v2**, `FAILED_RUNTIME_CHECK` | v0 r0, 0.6 / 0.7 s | v0 r0, 0.2 s | `FAILED_RUNTIME_CHECK`, no resume |
| 6_in_flight | finished, not aborted | same | same | same |
| 8_cancel_release | nothing waiting | nothing waiting | seek and release interrupt the waiting open (`InterruptedIOException`), release 15 / 19 ms | nothing waiting |
| 9_long_hold | `TIMEOUT` | `TIMEOUT` | `TIMEOUT` | `TIMEOUT` |

Readings:

- `FAILED_RUNTIME_CHECK` is Media3's `StuckPlayerException`: the player is buffering, the buffer is empty and nothing is
  loading for 4 s. A refusing `LoadControl` triggers it, so it rules out K1 and K1_K3.
- In 3_redirect, the request to the redirect target was sent after the close for every candidate.
  `HttpURLConnection` follows the redirect inside an `open()` that had already passed the gate.
- In 6_in_flight the transfer under way was finished for every candidate. The resume time there (about 6.6 s) comes
  from the throttled transfer, not from the hold.
- In 9_long_hold no hold avoids the no-progress detector. The controller pauses the player once the buffer runs out
  behind the closed gate, and `ConsentGateTest` runs with the detector cut to 3 s to show the pause works.

## Reproducing

```powershell
uv run python tools/android/controlled_server.py --port 8765   # separate terminal
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
$adb = "$env:ANDROID_HOME\platform-tools\adb.exe"
& $adb reverse tcp:8765 tcp:8765
cd apps/android
.\gradlew :app:installEmulatorDebug :app:installEmulatorDebugAndroidTest
& $adb logcat -c
& $adb shell am instrument -w -e t9trial true -e class pl.altani.spike.playback.TransferGateTrialTest `
    pl.altani.spike.test/androidx.test.runner.AndroidJUnitRunner
& $adb logcat -d -s T9:I    # one JSON line per candidate and case
```

The trial asserts nothing; without `-e t9trial true` its 40 cases are skipped. The checks of the chosen hold run in
the default suite:

- `ConsentGateTest` (controller and bridge): hold and resume on consent or Wi-Fi, the deferred `stream_refresh`, a user
  pause, and the resync after lost events.
- `ConsentUiTest` (the Activity): the consent button, the next episode, Home and the lock while held.
