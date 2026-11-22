# ADR-0004: Local state model for watching, the download queue and the offline library

## Status

Proposed

## Context

The Android MVP ([requirements](../product/android-mvp.md)) needs durable state the CLI never had: per-episode
watch progress separate from the watched flag, a persistent download queue that survives restarts and network
changes, a library of downloaded files, and shared preferences. ADR-0002 kept history to a per-series resume cursor
(`last_ep`) and a set of "downloaded" episode numbers, and noted that real playback progress would be a later schema
version. This is that version (schema 2). Stage 1 delivers the model, the migration and the atomic operations; the
executor that runs the queue, file handling and the bridge come in later stages.

## Decision

### One database, one migration path

All state lives in the existing SQLite file (`history.db` for the CLI; the Android adapter picks its own path) and
is migrated by the same `user_version` mechanism. One file means one source of truth about the queue and
transactions that span history, jobs and offline items. `alt_ani_core.state` holds the stdlib-only models;
`alt_ani_core.history.{database,migrations,watch,queue,library,preferences,local}` hold the stores;
`alt_ani_core.rules.{criteria,job_state,network}` hold the pure rules. `open_local_state(path)` returns every
store on one `Database`. `sqlite3` stays confined to an explicit list of modules (`tests/architecture`).

Every operation is one `BEGIN IMMEDIATE` transaction on its own connection. Writes are column-scoped; checks that
guard a change are made inside the same transaction. Nothing in the database is a secret: stream and embed URLs,
headers, cookies and tokens are never stored, and the JSON identity, validator and error columns refuse any value
containing `://`.

### Identities

- An episode is `EpisodeKey(series_id, number)`, like the existing `downloaded_episode`. The `episode` table keeps
  its title and Shinden page URL (the browser fallback). Renumbering on Shinden would split keys — accepted for
  compatibility with the existing data.
- A source is `SourceIdentity`: the per-episode `online_id` plus the normalised fields of `PlayerFingerprint`
  (host names, never a URL). A variant is `VariantIdentity(label, height, protocol, container)`. The result of
  resolving them (`StreamVariant.url`, headers) is short-lived and never persisted.
- A file is a `StorageLocator(backend, key)`, opaque to the engine: a path for `fs`, a content URI or another key
  for a platform adapter.

### Watch state

`watch_state` has three independent groups of columns: the last opening (`last_opened_at`, `opened_via`), the
position (`position_ms`, `duration_ms`; NULL means unknown, not zero) and the watched flag (`watched`,
`watched_source`). Opening in a browser touches only the first group.

Position writes are fenced by playback session: `begin_playback` creates a session and supersedes any earlier one
of that episode; `record_position(session, seq, …)` is applied only for the current session and a `seq` greater than
the last one kept (the first write of a session is `seq` 1). The value itself is never compared, so rewinding and starting over are kept, while a late write
from a superseded player is refused. `record_playback_ended` is the only automatic way to mark an episode watched.
A superseded session that really reached the end does not mark it either — a predictable rule; the manual change is
always available.

### Criteria

`DownloadCriteria(audio, subs, quality)` is copied into each job when it is added. Audio is `any` or a language;
subtitles are `any`, `none` (the source declares no subtitles — an empty declaration is unknown, not absent) or a
language. Quality is best available or `max:<height>`, an upper limit that variants of unknown height never satisfy.
`match_variant` returns `NoMatch` instead of falling back to the extractor's default; `Stream.select` keeps its
behaviour for the CLI. A compatible alternative is another source that satisfies the job's stored criteria.

### Download jobs

States: `queued`, `running`, `stopping`, `finalizing`, `paused`, `waiting` (with a reason: `network`,
`interaction`, `storage`, `source_decision`), `failed`, `completed`, `cancelled`. `paused` is only ever a user
decision; `waiting` only ever a condition. A partial unique index allows one open (not completed or cancelled) job per
episode — a `failed` job blocks a duplicate until it is retried or cancelled. Adding skips episodes with an open job or
an *available* offline item; a legacy `downloaded_episode` record or a `missing` item is not a reason to skip.

**A row change does not stop I/O.** A claim gives the executor an `attempt_id`; every executor write is fenced by it.
Pausing, cancelling or holding a running job only moves it to `stopping` with a target; the attempt keeps its slot
(the active limit counts `running`, `stopping` and `finalizing`) and the job cannot be claimed again until the
executor calls `ack_stopped` after its I/O has ended. `report_progress` answers `CONTINUE`, `STOP` or `LOST` (the
attempt no longer owns the job). Publishing the file needs `begin_finalize`, refused once a stop was requested or the
attempt was lost; only a `finalizing` attempt can `complete`, or end in `failed` or `waiting` (e.g. a full disk while
publishing; the target stays recorded). Cancelling is final; a user pause outranks a wait; resume undoes only the
user's own pause. When an attempt ends on its own (`wait`, `fail`) while a stop is pending, a user's pause or cancel
still wins, but over a scheduler's request (a network hold, or a hold already released) the executor's own outcome
wins — an interaction it needs or a failure it hit is never turned into a plain retry. Executor calls never raise for
a lost attempt or a state they cannot apply to; they return `False`/`None`/`LOST`. The engine guarantees no second attempt and no finalisation after a stop;
it cannot guarantee that no byte moves after the request — the executor bounds that window by checking at every
progress callback.

`claim_next(network)` refuses jobs the network rule does not allow (Wi-Fi always; mobile data with the job's own
consent or the global `any` policy, read live). Deciding which jobs to hold or release on a network or storage change
is the scheduler's (stage 2), through the `hold`/`release` primitives; a `release` never lifts a pause.

### Resuming a partial file

Same source and variant only make a partial a *candidate* (`resume_candidate`). The executor records a
`ResourceValidator` of non-secret evidence (lengths, counts, digests of an ETag or of a URL-free segment list) and,
after refreshing the URL, resumes only if `same_resource` holds; otherwise it starts over. Another source or variant
always drops the partial.

*Superseded in part by [ADR-0005](0005-offline-executor.md):* the evidence must be a strong validator checked in the
very request that adds bytes, together with a durable checkpoint; without it the job waits for a source decision
instead of starting over, unless the compatible-alternatives policy authorises it.

### Offline items and the file system

An offline item is created only by `complete`, in the same transaction that completes the job. If a `missing` item of
the same episode holds the same locator (a re-download to the same path), its row is reused and becomes available
again; any other holder of the locator — an available item, an item being deleted, another episode — is a
`state.locator_in_use` conflict, and the executor picks another target or ends the attempt. It records what was
actually saved and stays independent of the job (`ON DELETE SET NULL`) and of watch state. Deleting is two-step
(`mark_deleting`, file removal, `forget`). Exports are not tracked: an export is an independent copy by definition.

SQLite and the file system are never updated atomically together. The contract for stage 2 reconciliation: the target
locator is recorded before the transfer writes there and the file is published before `complete`; after a crash a
`finalizing` job is checked against its target, an item without its file becomes `missing`, a `deleting` item is
finished, and a file without a row is only reported, never removed.

*Amended by [ADR-0005](0005-offline-executor.md):* the target is reserved by `set_target`, which refuses a locator held
by another open job, an item (except a missing copy of the same episode) or a pending removal, and `mark_deleting`
refuses a locator an open job has reserved — a conflict is caught before publishing, not by `complete`. Items record
their SHA-256; a missing item becomes available again only when the digest matches. Files the store stops referencing
are queued in `pending_removal` in the same transaction; only those are removed automatically.

### Restart

One process executes the queue per database file, enforced by the executor ownership lock of
[ADR-0005](0005-offline-executor.md): `recover_interrupted(owner)` and `claim_next(network, owner)` refuse without it.
After acquiring ownership the owner calls `recover_interrupted` before claiming anything: `running` jobs are re-queued with `job.interrupted` (partial kept),
`stopping` jobs settle on their target, and `finalizing` jobs get a fresh attempt for reconciliation, which makes the
old attempt `LOST`.

### Migration from schema 1, `-D` and downgrade

Schema 2 only adds tables; `series`, `downloaded_episode` and `meta` (including the `legacy_json_*` import keys) are
untouched, and nothing is derived from `last_ep` or `downloaded_episode`.

Before upgrading from schema 1 the engine writes a verified copy:

1. a connection takes `BEGIN IMMEDIATE` and reads `user_version` (a second instance waits for the lock and then
   finds schema 2);
2. temporaries of an interrupted earlier attempt are removed;
3. a **second** connection copies the database with the backup API into `history.db.schema-1.<UTC>.bak.tmp`
   (the backup API stalls when run on the connection that holds the write lock — confirmed by a spike on
   Linux, SQLite 3.53.1; a reader may coexist with a reserved lock, so the copy is exactly the pre-upgrade state);
4. the copy is checked (`integrity_check`, `user_version`, the same tables and row counts as the locked source);
5. it is fsynced and renamed to `history.db.schema-1.<UTC>.bak`, and on POSIX the directory is fsynced so the name
   survives a power loss;
6. the schema steps run and the transaction commits.

A failure in steps 3–5 rolls back and raises `history.backup_failed`: the upgrade is fail-closed. Existing backups are
never trusted or overwritten — every upgrade writes its own.

`clear()` (`-D`, and the bridge's `history_clear`) keeps its old scope: it clears the resume cursors and download
records and removes series rows nothing else refers to. Watch state, jobs, offline items, preferences and `meta` stay.

A release with schema 1 refuses a schema 2 file (`history.newer_schema`) without modifying it. Going back means
restoring a `history.db.schema-1.<UTC>.bak` copy (losing what was recorded after the upgrade); `history.json` is still
untouched.

## Consequences

Easier:

- the mobile client and a desktop executor share one tested model of watch progress, the queue and the library;
- stage 2 can implement the executor against fenced, atomic operations without designing state again.

Harder / costs:

- the backup runs under the write lock; on a large history and slow storage another writer waiting longer than the
  5 s busy timeout gets `database is locked` once, during the one-time upgrade;
- every `report_progress` is a committed write; the stage 2 executor has to throttle it (e.g. once per second or per
  few percent), or it competes with UI writes for the lock and wears flash storage;
- the queue's correctness depends on the executor honouring `STOP`/`LOST` and `begin_finalize`;
- backups accumulate with each upgrade of a schema 1 file and are not removed automatically;
- a full disk blocks the upgrade (and therefore the start) until space is freed.
