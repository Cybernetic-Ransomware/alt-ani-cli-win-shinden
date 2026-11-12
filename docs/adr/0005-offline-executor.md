# ADR-0005: Offline executor — ownership, scheduling, request-bound resume and file publishing

## Status

Proposed

## Context

[ADR-0004](0004-local-state-model.md) gave the download queue and the library fenced, atomic store operations, but
left their execution to stage 2 ([requirements](../product/android-mvp.md)). Executing them safely needs decisions the
store alone cannot make: which process may run the queue, when a partial file may be continued, how a file is
published without overwriting anything, and what a restart may conclude from the files it finds.

Spikes (Linux, yt-dlp 2026.08.19, throwaway data) showed that yt-dlp cannot be trusted with these decisions on its own:

- a server that ignores `Range` makes it restart a progressive `.part` from zero silently;
- content that changed under the same length is appended to the old bytes (a splice);
- a native HLS download with a missing `.ytdl`, or one written behind `.part` (a crash between appending a fragment
  and recording it), appends duplicate fragments;
- a stop requested while a read is stalled is only noticed when the socket times out.

## Decision

### Ownership

One process executes the queue of a database file: the owner of an OS lock on `<db>.executor-lock`
(`alt_ani_core.history.ownership`; `flock` on POSIX — not `fcntl`/`lockf`, which any closed descriptor drops —
`msvcrt.locking` on Windows). The system releases it when the process dies; there is no lease, so a stalled owner keeps
it rather than losing it in the middle of its I/O. `claim_next` and `recover_interrupted` take the ownership and refuse
(`state.executor_not_owner`) without it. A second `OfflineQueue.start` raises `executor.busy` and writes nothing; that
process may still run user actions through the stores, which the owner sees by polling. The owner releases the lock
only once no attempt thread is alive. A database on a network share is unsupported.

### Scheduler and attempts (`alt_ani_core.services.offline`)

One scheduler thread and one thread per attempt. The scheduler sleeps on a condition variable: 250 ms while attempts
run (the monitor), otherwise until the next deferred job is due or a 5 s poll for other processes' changes; in-process
actions, network changes and finished attempts wake it at once. An idle pass only reads: `claim_next` (a write
transaction) runs only when some job may be claimed.

Each pass: the monitor stops attempts whose job is `stopping`, lost, or no longer allowed on the current network
(placing a `hold`); conditions are applied (network first, then storage: one stored wait reason is released and the
others re-checked before any claim, so Wi-Fi coming back never lifts an interaction, a source decision or a full
disk); abandoned files recorded in `pending_removal` are removed; jobs are claimed up to the limit.

An attempt ends in exactly one store operation, called after the transfer has returned (yt-dlp context closed, handles
collected). Stop checks are separate from persistence: every progress callback and every yt-dlp retry message checks the
attempt's cancel token; progress is written at most every 2 s; durable checkpoints every 5 s or 32 MiB. Without store
reads for 10 s (any read of a pass, preferences included) the executor stops transfers it can no longer confirm are
allowed. An outcome write that still fails after its bounded retries stays with the scheduler, which keeps the slot and
retries it every pass until the store accepts it. An attempt whose I/O does not end within `2 × socket timeout + 10 s`
keeps its slot; the process exit releases ownership and the next start recovers it. `close` releases ownership only once
the scheduler loop and every attempt's I/O have ended; otherwise it returns `False` and may be called again to wait.

`defer` (schema 3) returns a running attempt to the queue with a `not_before` and spends one of three automatic
retries (30 s, 2 min, 10 min); user actions (`retry`, `resume`, `choose_source`) reset the budget. A stop pending
while an attempt defers settles exactly as `ack_stopped` would, so closing the executor keeps pauses, cancellations and
the scheduler's waits instead of turning them into `queued`.

Failures map to store operations by category only (never by message): a full disk waits for storage; a network the job
may not use waits for the network without spending a retry; timeouts, 5xx/429 and unavailable sources defer and then
fail; denied access fails; an interaction need waits for interaction (the source is kept in the error parameters, the
grant lives in memory only); no source or variant matching the job's criteria waits for a source decision — never a
fallback past the criteria.

Storage waits are released only on evidence: free space for the known remainder plus a reserve, or the platform saying
space was freed (`notify_storage_changed`). Until then no other condition replaces the stored storage wait, so it
survives a lost network and a restart; once lifted, the job's other conditions are checked before it is queued.
Nothing else is ever deleted to make room.

FlareSolverr is used only when the configuration allows it **and** the device is on Wi-Fi.

### Resuming: request-bound evidence and durable checkpoints

A partial is continued only when both hold:

1. the database holds a checkpoint (length, and for HLS the fragment count) that never points past data already
   fsynced — the transfer reads it inside a progress hook (where yt-dlp has finished appending and recording the
   previous fragment), fsyncs the file and only then writes the checkpoint;
2. every response that adds bytes is compared, in that same request, with a strong validator recorded before the
   first byte:
   - progressive: the SHA-256 of the strong ETag and the total from the first transfer response. A resume learns the
     current ETag with a HEAD (only its hash is stored), sends it as `If-Range`, and the response must be 206 with the
     same start, total and ETag. A subclass of `YoutubeDL` overriding `urlopen` checks this before yt-dlp opens the
     file, so a refusal leaves the partial untouched. An output whose size differs from that total is a failure, never
     a result;
   - HLS (unencrypted, without byte ranges or init maps): a manifest of the SHA-256 of every segment's strong ETag,
     taken by HEAD requests before the first byte and fsynced in `validators.json` (the database keeps its digest);
     every segment response, retries included, must match its entry, and the segments already appended are
     revalidated before resuming. Replacing segments behind an identical playlist is therefore caught.

Only an answer about the resource refuses a resume: a HEAD with another or no strong ETag, or 405/501 (no HEAD at
all). A probe that got no answer (transport error, timeout, any other HTTP error) is a transfer failure for the retry
policy, and the partial and its checkpoint stay for the next attempt.

yt-dlp's own state is rebuilt from the checkpoint, never trusted: `.part` is truncated to the checkpoint length,
leftover fragments are removed and `.ytdl` is rewritten with the checkpoint's fragment index. A missing, corrupt,
lagging or advanced `.ytdl` is thus handled by the same rule.

Without such evidence (no strong ETag, any segment without one, encrypted HLS, DASH, a partial shorter than its
checkpoint, a changed playlist) the job waits for a source decision. Only the compatible-alternatives policy under
automatic selection authorises starting over (the same source first, then compatible alternatives); manual selection
and the default policy always ask. A partial with no durable byte is simply restarted: nothing a resume could keep is
lost.

The guarantee rests on HTTP strong-validator semantics (RFC 9110 §8.8.1): a server that changes bytes without changing a
strong ETag is not detected.

### Storage adapter (`alt_ani_core.storage`)

`FileStore` is a protocol without Android dependencies: work areas are local directories (the transfer writes there);
published files are opaque locators; `publish` puts the file exactly at the target or raises `storage.target_exists`.
A backend that cannot publish without possibly replacing a file fails `check_root` with `storage.no_atomic_publish` and
is not used.

`FsFileStore(root)`: partials in `root/.work/<job>/<partial>/` (created only by the executor), published files in
`root/<Series> [<id>]/<Series> - ep<N>.<ext>` with keys as relative POSIX paths, names reduced to a safe character set
(reserved Windows names, trailing dots, 120-byte components, numbered on collision). Keys escaping the root, also
through symlinks, are refused. Publishing is a hard link on POSIX (`EEXIST` is atomic), then a directory fsync, then
unlinking the work name; on Windows a plain rename (refused when the target exists). A POSIX root without hard links
(vfat, exFAT) is refused. Windows has no directory fsync: the durability of the new name rests on NTFS's journal.

### Locations, publishing and deletes

The target is reserved in the database (`set_target`) before publishing; the reservation is refused when another open
job targets it, any offline item holds it (except a missing copy of the same episode) or it awaits removal.
`mark_deleting` is refused for a locator an open job has reserved. Each side checks the other's record inside a
`BEGIN IMMEDIATE` transaction before touching the file, so neither removes or overwrites the other's file.

Order of a completion: transfer → fsync → SHA-256 → evidence recorded as the partial's validator (`kind=complete`) →
reservation → `begin_finalize` → publish → `complete` (which stores the digest on the item and queues the work directory
for removal in the same transaction). SQLite and the file system are never updated atomically together.

### Recovery

After ownership and before any claim, idempotently: `recover_interrupted`; each job found finalizing is completed only
when the target, or the work file then published, matches its recorded size and SHA-256 (a foreign file at the target
fails the job and stays untouched; no evidence fails it); interrupted deletes are finished; an available item without
its file (or with another size) becomes missing; a missing item becomes available again only when the file's SHA-256
equals the recorded one — size never proves identity; `pending_removal` entries are removed; every other file or work
directory under the root is only reported (`OrphanFound`).

### Library

Deleting is per item (`mark_deleting` → remove → `forget`); the check, removal and `forget` hold one OS lock beside the
database (`<db>.delete-lock`), shared by every process, and re-read the row under it. A deep verification compares the
SHA-256 of an available copy too and marks a mismatch missing. A failed removal keeps the row `deleting` for a retry; a
file that no longer is the recorded copy is left in place and reported. Exports are streamed copies (never links) into
a temporary file, fsynced, then published without replacing anything (numbered on conflict, or refused). Watch state
is never touched. Bulk operations report each item.

### Schema 3

`download_job.retry_count`, `download_job.not_before`, `offline_item.sha256` and the `pending_removal` table. Users of a
release with schema 1 upgrade 1 → 3 behind one backup; a build with schema 2 (development only) refuses the file.

## Consequences

Easier:

- the platform adapters of stage 6 implement `FileStore`, `NetworkSource` and `SourceProvider` against a tested
  executor;
- a crash at any step of transferring, publishing, deleting or recovering has a defined, tested outcome.

Harder / costs:

- hosts without strong ETags never resume automatically; their jobs wait for a decision (or restart under the
  compatible policy);
- the HLS segment sweep costs one HEAD per segment before a fresh transfer and may trip a host's rate limit, which only
  makes that job non-resumable;
- rewriting `.ytdl` depends on yt-dlp's private format; a version that changes it makes resumes wait for a decision;
- stopping is bounded by the socket timeout (15 s) and, while resolving, by the HTTP timeout; the slot stays taken
  until then;
- completing a download reads the file once more to compute its SHA-256;
- desktop tests do not show that background work behaves correctly on Android; the Windows lock and fsync behaviour
  rely on CI runs on Windows.
