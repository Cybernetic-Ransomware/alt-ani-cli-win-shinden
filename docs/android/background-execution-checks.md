# Background execution checks

Checks for [ADR-0008](../adr/0008-android-background-execution.md). Results: `PASS`, `FAIL`, `SKIPPED` (does not
apply to this API level) or `NOT_EXECUTABLE` (applies, but the device or image cannot carry it out). Every transfer runs
against `tools/android/controlled_server.py`; no real host is needed.

## Automated

```powershell
uv run python tools/android/controlled_server.py --port 8765   # separate terminal
adb reverse tcp:8765 tcp:8765
adb shell am instrument -w -e controlledServer required -e class pl.altani.spike.offline.background.BackgroundRunTest `
    pl.altani.spike.test/androidx.test.runner.AndroidJUnitRunner
uv run python tools/android/background_kill.py
adb shell run-as pl.altani.spike cat files/offline-outcomes.jsonl
```

| Check | Test | Path |
|---|---|---|
| Transfer goes on after Home | `aTransferGoesOnAfterHome` | both |
| Screen off, device forced idle | `aTransferGoesOnWithTheScreenOffAndTheDeviceIdle` | both |
| 10 Activity recreations: one session, engine, executor | `recreatingTheActivityKeepsOneSessionOneEngineAndOneExecutor` | both |
| Stop during held I/O keeps ownership and the component | `theNotificationStopDuringHeldIoKeepsOwnershipAndTheComponentUntilTheIoEnds` | both |
| Pause ends the session, partial kept | `pausingTheOnlyJobEndsTheSessionAndKeepsItsPartial` | both |
| Cancel keeps the session for the next job | `cancellingOneJobKeepsTheSessionForTheNextAndRemovesOnlyItsPartial` | both |
| Engine offline → Wi-Fi, resume by range | `goingOfflineHoldsTheJobInTheSessionAndTheNetworkComingBackResumesIt` | both |
| UIDT stopped by lost connectivity, restarted | `aUserInitiatedJobStoppedByLostConnectivityIsRestartedByTheSystemWhenItReturns` | API 34+ |
| Restricted standby bucket leaves a consistent state | `theRestrictedStandbyBucketLeavesAConsistentState` | both |
| Denied `POST_NOTIFICATIONS` | `aDeniedNotificationPermissionDoesNotStopTheDownload` | API 33+ |
| Nothing below API 30 | `belowApi30NothingIsLaunched` | API < 30 |
| Kill -9 mid-transfer, recovery, one copy | `BackgroundKillTest` via `background_kill.py` | both |

`background_kill.py` runs `arm` without waiting for it: `arm` holds a transfer, writes its pid to
`files/background-kill-armed` and stays alive. The script fails unless that pid is alive before the kill, `run-as kill -9`
exits 0, the pid is gone afterwards and `arm` did not pass on its own; only then does it run `verify`.

Every check that ends a session also waits for the component to be gone (no session, no scheduled job or running
service, no notification `52001`). A kill inside `finalizing` is covered by the core's recovery tests: the server can
hold a response, not the moment the engine enters `finalizing`, so the device test does not claim it.

The idle check uses loopback through `adb reverse`, which Doze does not firewall; it shows that the session and the CPU
keep going, not that a real network stays open in Doze.

## Manual: Mi 10T Lite (Android 12, service path)

| # | Steps | Expected |
|---|---|---|
| M1 | Throttled transfer (`throttle_bps`), Home, lock the screen 15 min; default MIUI battery saver, then "No restrictions" | transfer finishes or recovery resumes it; `uiautomator dump` / `adb logcat -s AltAniOfflineRun` |
| M2 | Swipe the app away from recents during a transfer | MIUI may kill the process; next "Resume" recovers with a range request |
| M3 | Wi-Fi off 2 min, then 12 min | 2 min: resumes by itself; 12 min: the session ends after 10 min with "needs your action", nothing restarts |
| M4 | Force-stop in settings during a transfer | no restart; "Resume" recovers |
| M5 | Reboot with a queued job | nothing starts; the queue is kept |
| M6 | Stop in the Task Manager | `NOT_EXECUTABLE`: Android 12 has no foreground service Task Manager (emulator 33+) |
