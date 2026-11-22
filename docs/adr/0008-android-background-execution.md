# ADR-0008: Android background execution

## Status

Proposed

## Context

[ADR-0007](0007-android-file-store.md) left background work out: a cached or frozen process stops the executor, and
the next start recovers. The MVP ([android-mvp](../product/android-mvp.md)) asks for downloads that go on after the
user leaves the app or locks the screen. The engine stays the only scheduler ([ADR-0005](0005-offline-executor.md)):
one executor per database (`<db>.executor-lock`, no lease), recovery as the only judge of an interrupted transfer,
`executor_stop` with `ended: false` keeping ownership while I/O runs. Android only decides whether the process may keep
running: no mechanism guarantees that a transfer finishes, so the design leans on recovery, not on the component.

The app supports API 24–36 (`targetSdk 36`); offline downloads need API 30. Android 12 forbids starting a foreground
service from the background; Android 14 requires a service type; Android 15 limits `dataSync` services to 6 h a day;
Android 16 counts regular, top-started and FGS-run jobs against runtime quotas, with user-initiated data transfer
(UIDT) jobs exempt. WorkManager's long-running workers are foreground services underneath, with the same limits and,
from Android 16, the job quota on top.

## Decision

- **Mechanism by API level, no fallback.** API 34+: a UIDT job (`JobScheduler`, `setUserInitiated`, a required
  network, `setPersisted(false)`). API 30–33: a `dataSync` foreground service, `START_NOT_STICKY`, with a partial wake
  lock. Below 30: nothing is launched. A refused UIDT schedule never falls back to a service. No WorkManager.
- **One coordinator.** `OfflineRun` (process singleton) owns sessions; both components are thin `RunHost` adapters.
  A session lives as long as its component: the component's start runs `executor_start`, its end `executor_stop`.
  Without a component the executor does not run, so there is no hidden background work. Activities never stop it, and
  `runtime_close`/`BridgeRuntime.shutdown` are never called by a session.
- **The engine decides what runs.** `RunPolicy` decides only whether the component stays, from a fresh `queue_snapshot`
  plus `preferences_get` after every event, after a resync, at a limit, and at least every 60 s (a stopped pump or lost
  events only delay a decision). Buckets: transfers (`running|stopping|finalizing`), due `queued`, deferred `queued`
  (future `not_before`), network waits, waits for the user (`interaction|source_decision|storage`).
- **When a session ends.** Something can run → it stays, whatever else waits. Only deferred jobs → it stays without a
  wake lock (the engine's backoff is bounded). Network waits → 10 min, then the service ends and asks the user; a UIDT
  job finishes with `reschedule` while inside the restart window and the scope still holds. Only waits for the user →
  30 s to settle, then the session ends with one "needs your action" notification. Nothing left → it ends at once.
- **Starts.** `OfflineRun.request()` from the visible UI is the only way to begin a session (the debug screen now, the
  queue screen in #53); concurrent calls share one check, read and launch. Opening the app reads state and starts nothing. A UIDT job may be restarted by `JobScheduler`
  within 24 h of the user's decision (`initiated_at`, `elapsedRealtime`, in the job's extras), checked in `attach`
  before `executor_start`; the window never cuts short a session that runs. Stopped by the user in the Task Manager,
  force-stopped or rebooted: no restart of ours.
- **Generations.** Every session has a generation; `onStopJob`, a service timeout or destroy, and the notification's
  Stop carry it, and a stale one is ignored. After a system stop the host is detached: the drain still stops the
  executor but never calls `jobFinished`, posts a notification (the session's or "needs your action") or touches a wake
  lock for it. Only the newest generation writes `RunState`.
- **Order across sessions.** The system may start the next component before the last drain ended (`onStartJob` right
  after `onStopJob`). The new host attaches at once, but its `executor_start` waits for every `executor_stop` of the
  previous drain, so an old stop attempt never lands on the new executor. No callback waits: the ordering lives in the
  coordinator's coroutines.
- **Stopping.** `executor_stop(10 s)` up to six times; `ended: false` keeps the component. After that the component ends
  and ownership stays with the engine until its I/O ends; a frozen or killed process releases the lock only through the
  OS, and recovery settles the rest. The notification's Stop sends the generation to a receiver that only hands it on
  (no `goAsync`: the running component keeps the process alive until the drain ends).
- **Network constraint.** `wifi_only` is `NET_CAPABILITY_INTERNET` with a Wi-Fi or Ethernet transport, as
  `DeviceNetwork.classify` counts Wi-Fi, never `NOT_METERED`; mobile data allowed by the policy or by an open job's
  consent is plain `INTERNET`. The scope is fixed at `request()`. Widening it during a session keeps restarts allowed
  (they wait for Wi-Fi, conservatively); narrowing it forbids a restart, because the wider constraint would restart a
  job with nothing to do; the next `request()` uses the new scope. The engine always applies the policy itself.
- **Wake lock.** Service path only: held while a transfer runs, plus at most 30 s for a due job to be claimed; released
  while anything waits; renewed with a 10 min timeout by each read. No Wi-Fi lock.
- **Notifications.** Channel `offline_downloads` (low importance). `52001`: the session, from the summary only (counts,
  sizes; determinate for one transfer of known size, indeterminate otherwise; no titles or URLs). `52002`: one
  dismissable "needs your action" after a session ended for the user, a network wait without restart, an expired window
  or a failed start. A denied `POST_NOTIFICATIONS` changes nothing about the work.

## Consequences

Easier: downloads continue with the app in the background and the screen off within Android's rules; no second queue or
scheduler exists in Kotlin; every end of a session, including a kill without callbacks, leaves recovery to decide.

Harder: a UIDT restart after a lost network depends on `JobScheduler` and has no promised time; on API 30–33 nothing
resumes after a session ended until the user asks; a frozen process still `closing` holds the database lock until it
thaws or dies; vendors that kill background processes (MIUI) can end a service early, which recovery absorbs but cannot
prevent. The service path's `onTimeout` is defensive only: it runs below API 34, where `dataSync` has no time limit.
Verified so far by the JVM suite and on one API 37 emulator (UIDT path, `BackgroundRunTest` and `background_kill.py`,
which fails unless its own kill -9 ended the process `arm` reported).
The service path (API 30–33), the Mi 10T Lite and Android 15/16 images are not yet verified (see
[background-execution-checks](../android/background-execution-checks.md)).
