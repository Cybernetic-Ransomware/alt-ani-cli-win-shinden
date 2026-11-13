# ADR-0006: Mobile client contract (bridge v2)

## Status

Proposed

## Context

Stage 3 of the [Android MVP](../product/android-mvp.md) needs one explicit, versioned JSON contract through which the
Kotlin client can reach everything the MVP needs: catalogue and sources, watching online and offline, watch history,
the download queue, the library, preferences and the network. Before this change the bridge (`alt_ani_bridge`, loaded
by Chaquopy from source) covered only the catalogue and episode sessions (v1). The engine already had the stores of
[ADR-0004](0004-local-state-model.md) and the executor of [ADR-0005](0005-offline-executor.md), but nothing reached
them through JSON. Everything here is verified on the desktop; nothing is claimed about Android behaviour.

The method, DTO, error and event reference is [`CONTRACT.md`](../../apps/android/app/src/main/python/alt_ani_bridge/CONTRACT.md).

## Decision

### Where the logic lives

Domain rules stay in the engine: choosing a local copy (`rules.criteria.item_matches`), why nothing matched
(`no_match_reason`), the next episode and selections (`rules.episodes`), which job actions apply (`rules.job_state.
available_actions`), the stream gate (`rules.network.stream_allowed`) and playback preparation
(`services.playback.PlaybackService`). The bridge parses and validates input, calls the engine, maps DTOs and errors,
and owns handles, the runtime and events. `tests/architecture` allowlists the engine modules the bridge may import and
forbids `sqlite3` and private names.

### Runtime, handles and configuration owner

A runtime is the process state of one database file (resolved path): `LocalState`, the file store, one
`StaticNetwork`, the executor (created on its first start), the event ring and its watcher, and the watching consents.
The first `create_engine` for a database creates the runtime and fixes its `storage`, `cache_dir`, executor `access` and
initial `network`; a later handle joins when these are omitted or equal, and gets `runtime.config_conflict {field}`
otherwise. A handle (`engine_id`) is a UI engine with its own access; `set_access` changes only that handle, and
`executor_set_access` only the executor (applied through `EngineSources.set_base_access` with the Wi-Fi-only solver
rule). `executor_status` never touches the OS lock, so reading it cannot make a concurrent start fail: its `owner` is
`self`, `other` (another runtime in this process, from the in-process registry) or `unknown` — an owner in another
process cannot be seen without contending for the lock, and only `executor_start` finds out (`executor.busy`). `create_engine` creates no HTTP transport: it is made on the first remote request, so every local operation
works where `curl_cffi` has no wheel.

One executor per database is guaranteed by the OS lock of ADR-0005 together with the process-wide registry in
`history.ownership`: a second `Bridge` instance, another runtime or another process get `executor.busy`. Closing a
handle or a session, or ending a poll, never stops the executor. `executor_stop` and `runtime_close` take a timeout;
`ended: false` means some attempt's I/O is still running, ownership is kept, the state is `closing`, `executor_start`
answers `executor.closing`, and the call may be repeated. `runtime_close` also waits for the runtime's own operations
(a cancelled export or delete may still be using the file store) and for every push thread, including those of handles
closed earlier: each close reports them until they end, and only then are the runtime and its platform objects released. `runtime_close` (and `Bridge.shutdown`) closes handles, then
the executor, then the watcher and push threads, then gives back the platform objects the runtime used; a timeout
never releases a lock. A platform object in use by a runtime or a running export cannot be released (`platform.in_use`).

### Repeating a mutation after a lost reply

A repeat must not have an effect the user did not decide on the current state. Every job action (`pause`, `resume`,
`cancel`, `retry`, `allow_cellular`, `choose_source`, `retry_interaction`), `watch_set_watched`, `preferences_update`
and `queue_reorder` require `expected_revision`, which the store checks in the transaction that writes
(`state.conflict {expected, found}`). A repeat of a write that succeeded meets the bumped revision and changes nothing;
the client refreshes and asks again, and the bridge never retries with a newer revision on its own. `queue_enqueue`
takes the preview's plan with its full criteria snapshot and `basis_seq` (the newest job sequence the preview saw): an
episode that got any job after it, even one cancelled since, is skipped as `changed_since_preview`. Playback positions
are fenced by the session `seq`; `playback_begin` replaces a session the client never learnt about; a browser opening
only moves the opening timestamp; a delete is per item id. **Export is not idempotent**: a repeat with `rename` makes a
second copy and with `fail` (the default) reports `storage.target_exists`; after a lost reply the client checks the
destination. There is no idempotency-key table.

### Cancellation boundaries

| Operation | Where `cancel(op_id)` acts | Result |
|---|---|---|
| Short SQLite mutation | before its transaction only (also a closed handle or session) | `cancelled` with nothing changed, or the write's own outcome once it started, even if its session closes before the reply |
| `executor_start` | during recovery | `cancelled`; ownership released; recovery steps already done are durable and idempotent |
| `executor_stop`, `runtime_close` | not cancellable | bounded by `timeout_ms` |
| `library_delete` | between items and inside a digest | the item in progress stays `deleting` (finished by `library_retry_deletes`); the rest is `not_attempted` |
| `library_verify` | between items and inside a digest | the item in progress is unchanged; the rest is `not_attempted` |
| `library_export` | while copying, before publishing | the temporary file is removed and nothing appears; a file already published is reported `ok` with its name |
| remote reads | as in v1 | `cancelled` / `session_closed` |
| `events_next` | at once | `cancelled` |

Batches always answer `ok: true` with one result per item, plus `interrupted` and `not_attempted` when cancelled.

### Events are refresh signals

An event says an entity has a newer state; intermediate states may be skipped. Entity events come only from the
watcher, which reads revisions (`queue.revisions()`, `library.revisions()`, the preferences revision) — every 500 ms by
default, only while someone listens, at once after an in-process mutation — and never writes. It publishes at most one
event per entity per pass; coalescing happens before a `seq` is assigned and a published event is stored as JSON text,
so it never changes. Deleted items are found by their absence (`library.removed`). Progress arrives as `job.changed`
with bytes, since `report_progress` bumps the job revision; the engine's own `JobProgress` and `InteractionRequired`
are not forwarded. Other engine events pass an allowlist of fields; an unknown type is dropped and counted.

`events_head` runs one watcher pass before answering, so the watcher's base is never newer than the head: every change
after the head is signalled with a later `seq`. The client's procedure (start, reconnect, `resync`) is head → snapshots →
`events_next` from the head, ignoring signals whose revision it already has. A different `epoch` (a new runtime) or
`resync: true` restarts it. Changes made by another process are seen the same way, because the watcher reads the
database. Pull (`events_next`, a long poll cancelled by `op_id`) is the base; the v1 push listener is now an adapter on
its own thread per handle, so a slow or throwing listener blocks or counts only itself and gets one
`events.resync_required` after falling out of the ring.

### Watching and mobile data

`playback_begin` records the player opening and starts a fenced session in one transaction. `playback_prepare` picks a
matching local copy first (others are only listed), then checks the network gate, then sources by the criteria
(`criteria.no_match {audio|subs|quality}`, never a wider language or the default variant past a limit). The bridge keeps
the choice — source id, `VariantIdentity`, criteria — in the source session, never a URL. A source session is bound to
its episode (number, page and the series of the first method that names one); handing it to another episode in
`playback_prepare`, `watch_open_in_browser` or `queue_choose_source` is refused as `other_episode`. `stream_refresh` checks the
network and consent again, resolves the same source afresh and keeps the exact variant, or uses the kept criteria when
it is gone, or fails with `no_match`. Consent for mobile data covers the open viewing (P3): it survives a source change,
a refresh and rotation, ends with `playback_ended`, `playback_close` or a `playback_begin`, and lives in the runtime only.

The bridge cannot stop requests Media3 makes with a URL it already has. After `network_set(cellular)` it emits
`playback.consent_required` for each open viewing without consent; the client must then stop new player transfers,
let the buffer play and not refresh until `playback_allow_cellular`. Whether Media3 can be held like that is checked
in stage 5.

### Platform boundary and secrets

The file store is either `{"kind": "fs", "root"}` or a registered host object (`Bridge.register_platform_object`,
checked by duck typing). `FileStore.playback_target` returns a `file` path or a `content_uri`. Export writes through
`ExportTarget.create(name, on_conflict) -> ExportWriter(write, commit, abort)`; `DirectoryExportTarget` keeps the
guarantees of the old `export_copy`. Stream URLs and headers appear only in `playback_prepare`, `stream_refresh` and
v1 `resolve`; an embed URL only in `watch_open_in_browser`. They never appear in events, errors, other DTOs, the
database, diagnostics or runtime state. No method accepts an interaction grant (`capabilities.interaction_grants:
false`); `queue_retry_interaction` gives a job another attempt without one.

### Errors and the v1 change

`code` values are unchanged. `kind` becomes a closed set chosen by exception class: `invalid_input`, `not_found`,
`conflict`, `no_match`, `waiting`, `cancelled`, `closed`, `unavailable`, `access`, `storage`, `limit`, `internal`. Only
the bridge's own input errors (and unparsable JSON) are `bad_request`; any other `KeyError`/`TypeError`/`ValueError` is
`internal`. Requests reject unknown fields, `NaN`/`Infinity`, booleans as integers and inputs over the limits in
`bridge_info.limits`. Changes visible to a v1 caller, which has no consumer yet (the app runs only the feasibility
check): the new `kind` values, push events in the v2 format delivered on a separate thread, the transport error moving
from `create_engine` to the first remote call, strict input checks and the extra `delivery_ended` field of
`close_engine`.

### Spikes (2026-11-13, Linux 7.0, Python 3.14.6; scripts not committed)

| # | Mechanism | Result |
|---|---|---|
| S1 | ring + long poll; push thread with a listener blocked on an event | passed: producer passed 40 publications while the listener was blocked, one resync after overflow, cancel ended a waiting poll |
| S2 | `op_id` in a `ContextVar` | passed: correct per call under a barrier; pool threads (`metadata_many`) see `null` unless the context is copied — kept as `null` |
| S3 | revision watcher with another process changing the database | passed: signals with revision ≥ the change and `library.removed`; no write (file digest unchanged); no reads without a listener |
| S4 | export target with a gated `write`, a fake platform target and a directory | passed: nothing at the destination or temporary after a cancel before commit; a cancel after commit reports the file |
| S5 | second owner in the process and in another process; stop with a stuck attempt | passed on Linux: both refused; `close(timeout)` false keeps the lock, true after the I/O ends. Windows (`msvcrt.locking`) runs in CI only |

## Consequences

Easier: the Kotlin client has one contract for the whole MVP, testable on the desktop; delayed repeats cannot undo a
newer decision; a slow UI never slows the engine.

Harder: every mutation needs the revision the client last read; signals require the snapshot-plus-cursor procedure;
the watcher costs one light read per interval while someone listens; export repeats need a client-side check.

Assumptions for stages 4–6, not verified here: Chaquopy callbacks from Python threads and a blocking `events_next` on
a Kotlin thread; Kotlin objects as `FileStore`/`ExportTarget` and the cost of `write(bytes)` over JNI for SAF;
`content_uri` and headers in Media3; holding Media3 loading until consent; network classification; `flock` in the
app directory; background work; a transport without `curl_cffi`.
