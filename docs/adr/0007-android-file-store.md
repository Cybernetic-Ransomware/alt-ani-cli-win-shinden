# ADR-0007: Android file store

## Status

Accepted

## Context

[ADR-0005](0005-offline-executor.md) refuses a POSIX root on which `publish` could replace a file, and `FsFileStore`
publishes by hard link. App storage on Android denies `link()` (SELinux, `EACCES`), so the core store fails
`check_root` with `storage.no_atomic_publish` there. Until PR #51 the production app created its engine without
`storage`: downloads, the library and local playback only worked in the debug `ControlledEnvironment`, whose
`AppStorageStore` lived in `src/debug/python/controlled_host.py`.

The store must keep every guarantee of ADR-0005 (no replacement, atomic publishing or a readable refusal, paths and
symlinks confined to the root, only identified files removed) without new permissions and without guessing that a
system call exists. minSdk is 24; `renameat2` is in bionic from API 30, and below it an unlisted system call may end
the process with `SIGSYS` under the app seccomp filter instead of failing with `ENOSYS`.

## Decision

- **Python, not Kotlin.** `android_storage.AppStorageStore` (`src/main/python`) subclasses `FsFileStore` and only
  changes `check_root`, `publish` and `playback_target`. `work_path` must be a local directory for yt-dlp, and a Kotlin
  object would cost a JNI call per `digest`/`scan` chunk for nothing. Debug code (`controlled_host`, `storage_probe`)
  uses this same class.
- **Publishing** is `renameat2(RENAME_NOREPLACE)` (ctypes system call, aarch64 and x86_64): atomic and never replacing,
  `EEXIST` → `storage.target_exists`. `check_root` probes that rename in `.work/`; any `OSError` there is
  `storage.no_atomic_publish`. There is no fallback to `rename()`, copying or check-then-move.
- **API gate.** The host passes `Build.VERSION.SDK_INT` explicitly (`AppStorage.register`); Python never detects it.
  Below API 30 `check_root` and `publish` refuse with `storage.no_atomic_publish {reason: api_level}` without making the
  call; on Android a missing level is `api_level_unknown`. The gate does not claim the call is forbidden on every older
  system; it only avoids trying. The app runs from API 24, offline downloads need API 30 for now.
- **Location.** One library at `filesDir/library` (with `.work/` inside), on the same file system as the database and
  outside `cacheDir`. `data_extraction_rules.xml` and `backup_rules.xml` keep it out of Auto Backup; a database restored
  without it is a valid state: recovery marks the items `missing` and keeps watch state, preferences and jobs.
- **Bootstrap.** `AltAniApplication`'s endpoint factory registers the store on the process's `Bridge` and passes
  `{kind: platform, handle}` to the first `create_engine`. The handle lives as long as that `Bridge` and is never
  released: a failed `create_engine` is retried on the same endpoint, and a new `BridgeRuntime` makes a new `Bridge`.
- **Tidy-up.** `check_root` runs only in `OfflineQueue.start`, with executor ownership held, before recovery and before
  any attempt (guarded by `tests/offline/test_executor.py::TestStartOrder`). After a successful probe it removes the
  probe files a crash left (`.work/.probe-<hex>`) and empty `.work/<job>/` directories with `rmdir`. Nothing is
  recursive, symlinks are never followed, unknown names stay and failures never fail the check. Unknown files elsewhere
  remain orphans, reported and kept.
- **Executor control.** `ExecutorControl` (Kotlin) starts, stops and reads the executor. Concurrent calls of one kind
  share one bridge call that runs in the runtime scope, and a start and a stop never overlap. It adds no scheduler,
  retry or timer, and nothing starts it automatically in this stage.

## Consequences

Easier: the production app has a working store whose guarantees are checked on the device (`StorageDeviceTest` runs
`storage_probe` on the real file system and SELinux policy) and by the desktop suite (`test_android_storage.py`,
`test_fs.py` runs its shared cases on both stores, `test_offline_flow.py` drives the controlled server).

Harder: Android 7–10 devices get `storage.no_atomic_publish` for downloads until a safe path is shown for them;
the gate below API 30 is checked on the desktop and with an injected level on the device, not yet on an Android 7–10
image. `renameat2` under seccomp is verified on one device (Mi 10T Lite, Android 12, MIUI); other vendors' policies
stay unverified. Background execution is
not part of this decision: a cached or frozen process stops the executor, and the next start recovers.
