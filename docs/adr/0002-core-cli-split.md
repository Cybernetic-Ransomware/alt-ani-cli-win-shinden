# ADR-0002: Shared engine (`alt_ani_core`) with the CLI as one of its clients

## Status

Proposed

## Context

Everything lived in one package, `alt_ani_cli`: Shinden parsing, the player XHR protocol, embed-host extractors,
downloads, history, terminal menus, desktop paths and subprocess playback. Domain logic imported the terminal
(`ui.progress`), configuration was read from the environment at import time, `curl_cffi` was imported in 17 modules,
and `cli.py` and `flow/handlers.py` imported each other through function-level imports.

A second client is planned: an Android application (Kotlin/Compose, Chaquopy, Media3) that reuses the Python logic.
That is only possible if the logic can be loaded without a terminal, without desktop paths, without a specific HTTP
library, and can be driven — and cancelled — from another runtime's threads.

## Decision

### Layout

A uv workspace with two Python distributions and one host project:

- `packages/core` — distribution `alt-ani-core`, import `alt_ani_core`. Depends on `yt-dlp` only; `curl-cffi` and
  `selectolax` are extras (`alt-ani-core[curl,selectolax]`).
- `apps/cli` — distribution `alt-ani-cli-win`, import `alt_ani_cli`, command `alt-ani-cli` (unchanged for users).
- `apps/android` — Gradle spike plus `alt_ani_bridge`, a JSON facade over the engine.

Tests stay in one `tests/` tree for the whole workspace: they share fixtures (fake transport, isolated paths), and
the layer rules are enforced by import contracts and `tests/architecture`, not by where a test file lives.

No compatibility shims: `alt_ani_cli.shinden`, `alt_ani_cli.extract`, `alt_ani_cli.cli` and friends are gone.

### Layers (imports point downwards only)

```
alt_ani_cli            alt_ani_bridge
      \                    /
       alt_ani_core.services        Engine, EpisodeSession
       shinden/  extract/  download  history/  transport/{curl,clearance}  dom/
       transport/api  diagnostics  events  cancel  config
       models  errors  rules/*  redaction  messages          (standard library only)
```

The engine never imports the CLI, `rich`, `InquirerPy`, `argparse`, `platformdirs`, `yaml` or `subprocess`, never
reads the environment and never prints. `curl_cffi` appears only in `transport/curl.py`, `selectolax` only in
`dom/selectolax_backend.py`, `yt_dlp` only in `extract/ytdlp_resolver.py` and `download.py`, `sqlite3` only in
`history/sqlite.py`. `tests/architecture/test_boundaries.py` checks all of this on the AST (so imports inside
functions count) and in a fresh interpreter; import-linter contracts in `pyproject.toml` check the layer order, and
`tools/check_packaging.py` installs the engine wheel alone in a clean environment.

### Synchronous engine, driven from worker threads

All the underlying code blocks (`curl_cffi`, `yt_dlp`, `sqlite3`), and Chaquopy calls Python synchronously from
Kotlin threads. An `asyncio` engine would have meant rewriting every extractor without making yt-dlp cancellable.

### Contracts

- **Transport** (`transport/api.py`): `Transport.open(profile) -> HttpSession`; every `open()` is a new cookie jar.
  The Shinden session and extractor sessions never share cookies. Errors are `TransportError` subclasses whose text
  never contains the request URL (URLs carry tokens). yt-dlp keeps its own networking, configured only through
  `ExtractOptions`.
- **DOM** (`dom/api.py`): `Document`/`Node` with the operations the parsers use (`css`, `css_first`, `attributes`,
  `text`, `iter`, `next`, node identity). The backend is swappable with `dom.set_backend`; a second backend is not
  implemented.
- **Access policy**: the platform decides (`Engine.set_access(AccessConfig)`); the engine does not detect networks.
  A direct-only policy uses neither FlareSolverr nor a cached clearance. A protection page is `AccessBlockedError`.
- **Stream model**: `Stream(variants, default_index)`; each `StreamVariant` has its own URL, headers and an explicit
  `StreamProtocol` (HLS, DASH, progressive) set by the extractor. Players and the downloader take a variant. Stream
  URLs are short-lived and are never persisted.
- **Events** instead of printing (`events.py`); **errors** carry `code`/`params` with English technical text, and
  the CLI renders Polish for the codes it has always shown in Polish.
- **History**: `HistoryStore` protocol, SQLite implementation (schema versioned by `user_version`, a connection per
  operation, `BEGIN IMMEDIATE`, `foreign_keys=ON` on every connection). `last_ep` is a resume cursor reported by the
  application, not proof of watching; real playback progress would be a later schema version.
  Schema 2 adds it, together with the download queue and the offline library: [ADR-0004](0004-local-state-model.md).
- **Android bridge** (`alt_ani_bridge`, loaded by Chaquopy from source): a JSON facade over the engine's public
  services and rules only; it validates input, maps DTOs and errors and owns handles and events, while every domain
  rule stays in the engine. Contract 2 and its reasons: [ADR-0006](0006-mobile-client-contract.md).

### Concurrency, cancellation, staleness

- `EpisodeSession` serializes `resolve`, `probe_hosts` and `reload` with one lock; its XHR state, embed cache and
  budgets change only under it.
- `Engine` hands the Shinden HTTP session out as leases tied to a generation. `set_access` and `close` bump the
  generation; the old session is closed once its last lease is released.
- `metadata_many` gives every worker thread its own HTTP session; no session is shared between threads.
- Cancellation (`CancelToken`) and staleness are `EngineInterrupt(BaseException)`, so they pass through the broad
  `except Exception` in extractor fallbacks and through yt-dlp. They are checked before and after every request
  (also after a request that failed, so a transport error from a session that went stale is reported as the
  interrupt), before every candidate and every extractor fallback, in every wait and in yt-dlp's progress hook. An
  interrupted operation runs no fallback, tries no further source, records no host penalty, no success and no cache
  entry.
- A download commits only after one more cancellation check once yt-dlp returns: only then is a staged redownload
  promoted over the old file and `DownloadSaved` emitted. A cancel before that point (also one racing a downloader
  error) keeps the old file and discards the staging copy.
- The Android bridge registers every call as an operation, with or without a client `op_id`, and binds it to the
  episode session it looks up. `close_session` (and `set_access`, `close_engine`) cancels the operations bound to
  that session and makes them report `session_closed` instead of a result; other sessions are untouched. A request already in flight is
  not aborted — it ends by its own timeout (curl 30 s, FlareSolverr 70 s, yt-dlp socket 30 s) and its result is
  discarded.

### Limits kept from before, and their owners

| Limit | Owner | Scope |
|---|---|---|
| one token refresh | `EpisodeSession` operation | one `resolve`/`probe_hosts` call |
| one player-list reload | `EpisodeSession.reload()` | one session; opening the episode again is a new session |
| two forced fresh resolves per source | `EpisodeSession.resolve(force_fresh=True)` | one session |
| host health evidence (TTL 600 s) | `Engine.health` | one engine |

### History migration

On start the CLI imports `history.json` into `history.db` once per file content (SHA-256 recorded in `meta`):

- the JSON file is only read; a `history.json.pre-sqlite.bak` copy is written once;
- identifiers, episode numbers and timestamps are carried verbatim; invalid entries are skipped and counted;
- an existing database row wins on watch fields, downloaded episodes are merged;
- a transient I/O or database error records nothing, so the next start retries;
- an unreadable or wrongly shaped file is recorded as corrupt and retried only when its content changes;
- `-D` clears entries but keeps `meta`, so a cleared history is not re-imported.

## Consequences

Easier:

- a client other than the terminal can drive search, episode opening, source selection and stream resolving;
- transport, DOM backend and history store can be replaced without touching parsers or policies;
- the resolve loop, its limits and its concurrency rules are testable without a terminal.

Harder / costs:

- two distributions to version and build; CI has to build and install both wheels;
- error texts raised by the engine are English; Polish exists only where the CLI maps a code;
- the first Shinden request (and a FlareSolverr round-trip) now happens lazily at the first operation, not at start.

### Rollback

PRs to `develop` are squash-merged, so reverting is `git revert <squash commit>` on `develop` (one commit, no
`-m`). `history.json` was never modified, so the previous version works immediately — without entries recorded
after the migration, which exist only in `history.db`. Re-applying the change later imports nothing twice: the
recorded hash matches, and a JSON file changed in the meantime is merged with the database winning.
