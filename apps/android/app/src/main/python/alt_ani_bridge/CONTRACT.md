# Bridge contract 2.0 — reference

Decisions and reasons: [ADR-0006](../../../../../../../docs/adr/0006-mobile-client-contract.md).

## Calling

- `Bridge.create_engine(json, listener=None) -> json`, `Bridge.close_engine(json) -> json`,
  `Bridge.call(method, json) -> json`, `Bridge.cancel(op_id) -> bool`, `Bridge.shutdown(timeout=None) -> json`,
  `Bridge.register_platform_object(obj) -> json`, `Bridge.release_platform_object(handle) -> json`.
- Envelope: `{"ok": true, "data": …}` or `{"ok": false, "error": {code, kind, retryable, message, params}}`.
- Reserved request fields: `engine_id` (every method except `bridge_info` and `runtime_close` by `db_path`), `op_id`
  (optional; unique while running; makes the call cancellable), `contract` (optional; another major is refused).
- Unknown fields are refused. Absent means the default; `null` means "none/unknown" and is accepted only where noted.
- Units: `_ms` milliseconds, `_bytes` bytes, times ISO-8601 UTC, heights in pixels, episode numbers finite numbers.
  `retry_after` (v1 XHR errors) is in seconds.
- Limits (`bridge_info.limits`): request 1 MiB, string 4096 characters, generic list 5000, `item_ids` 1000, episodes
  5000, `job_ids` 10000, `events_next.max` 500, `wait_ms` 30000. Exceeding one: `bad_request {field, problem:
  too_large, limit}`.

## Errors

`kind` is one of `invalid_input`, `not_found`, `conflict`, `no_match`, `waiting`, `cancelled`, `closed`,
`unavailable`, `access`, `storage`, `limit`, `internal`.

| code | kind | params |
|---|---|---|
| `bad_request` | invalid_input | `field`, `problem` (`missing`, `null`, `wrong_type`, `unknown_field`, `unknown_value`, `out_of_range`, `too_large`, `not_finite`, `malformed_json`, `empty`, `not_in_listing`, `unknown_format`, `other_episode`, `no_playback_selection`, `invalid_criteria`, `invalid_preferences`, `unsupported_platform_object`, `wrong_platform_object`), `expected?`, `limit?`, `minimum?` |
| `unsupported_contract`, `unknown_method` | invalid_input | `requested`, `supported` / `method` |
| `unknown_engine`, `unknown_session`, `unknown_source`, `unknown_platform_object`, `state.unknown_job`, `state.unknown_item`, `state.unknown_episode` | not_found | the id |
| `state.conflict` | conflict | `expected`, `found` (+ `job_id` or `series_id`/`number`) |
| `state.invalid_transition`, `state.finalizing`, `state.locator_reserved`, `state.locator_in_use`, `operation_id_in_use`, `executor.busy`, `executor.closing`, `runtime.config_conflict {field}`, `runtime.closing`, `platform.in_use`, `playback.not_open` | conflict | |
| `criteria.no_match` | no_match | `reason`: `audio`, `subs`, `quality` |
| `network.cellular_consent_required`, `interaction.required {scope, host, online_id}` | waiting | |
| `cancelled` | cancelled | |
| `session_closed {session_id, closed_by}`, `engine_closed`, `stale_session` | closed | |
| `network.offline`, `transport_unavailable`, `timeout`, `network_error`, `http_status {status}`, `sources_exhausted {attempts}`, `xhr_*` | unavailable | `retryable` set where a retry may help |
| `access_blocked {block}` | access | |
| `storage.*`, `storage.not_configured`, `history.unavailable` | storage | |
| `episode.reload_limit`, `episode.fresh_resolve_limit` | limit | |
| `internal` (message: exception type only), `history.newer_schema`, `history.backup_failed` | internal | |

## DTOs

- **series** `{id, slug, title, url}`; **episode** `{number, title, url}`; **player** (v1) `{online_id, player,
  lang_audio, lang_subs, max_res, date_added, subs_author, source}`.
- **criteria** `{audio, subs, quality}`: `audio` `any` or a language; `subs` `any`, `none` or a language; `quality`
  `best` or `max:<height>`. One-off overrides take any subset.
- **variant identity** `{label, height|null, protocol, container|null}`; **stream variant** adds `url`, `mime`,
  `headers`.
- **playback_session** `{id, series_id, number}`.
- **watch** `{series_id, number, revision, last_opened_at, opened_via, position_ms, duration_ms, position_at, watched,
  watched_source, watched_at}`; a missing row is revision 0 with nothing known.
- **job** `{job_id, revision, queue_pos, series{id,title}, episode{number,title}, state, wait_reason, stop_target,
  criteria, alternatives, selection_mode, source, variant, cellular_allowed, attempts, retry_count, not_before,
  bytes_done, bytes_total, has_partial, last_error{code,params}, interaction{scope,host,online_id}|null,
  offline_item_id, created_at, updated_at, completed_at, actions[]}`; `actions` ⊆ `pause, resume, cancel, retry,
  allow_cellular, choose_source, retry_interaction`.
- **item** `{item_id, revision, series_id, number, status, file_name, size_bytes, audio, subs, height, variant_label,
  protocol, container, source_player, created_at, verified_at}`.
- **preferences** `{audio, subs, quality, download_network, auto_alternatives, max_active_downloads, revision}`.
- **batch result** `{results: [{item_id, result: ok|skipped|failed, code, file_name?}], interrupted?: "cancelled",
  not_attempted?: [item_id]}`.

## Methods

### Runtime and handles

| method | request | response |
|---|---|---|
| `bridge_info` | — | `{contract, methods, capabilities{interaction_grants:false, events_pull, events_push, platform_storage, platform_export}, limits}` |
| `create_engine` | `cache_dir, history_path, xhr_backend?, antibot_delay_sec?, access?{flaresolverr_url, use_flaresolverr}, diagnostics_dir?, storage?{kind:fs,root}\|{kind:platform,handle}, network?` | `{engine_id, contract}` |
| `close_engine` | `engine_id` | `{cancelled_operations, closed_sessions, delivery_ended}` |
| `engine_status` | — | `{sessions, running_operations, listener_errors, engine_started, events_dropped}` |
| `set_access` | `access` | `{dropped_sessions}` (this handle only) |
| `runtime_close` | `db_path?` (else the handle's runtime), `timeout_ms?` (10000) | `{ended, executor_ended, delivery_threads_alive}` |

### Catalogue (v1)

`search {query}`, `metadata {url}`, `metadata_many {urls}`, `episodes {series | url}`, `open_episode {episode}`,
`select {session_id, filter?{lang,subs,player_name}, purpose?}`, `resolve {session_id, online_id, candidate_ids?,
policy?, rank_by_health?, force_fresh?, quality?}`, `probe_hosts {session_id, candidate_ids?}`,
`reload_episode {session_id}`, `close_session {session_id}`, `history_watch`, `history_downloads`,
`history_record_watch {series, episode}`, `history_record_download {series, episode}`, `history_clear`.

### Preferences and network

| method | request | response |
|---|---|---|
| `preferences_get` | — | preferences |
| `preferences_update` | `changes{audio?, subs?, quality?, download_network?, auto_alternatives?, max_active_downloads?}, expected_revision` | preferences |
| `network_set` | `kind: wifi\|cellular\|offline` | `{kind, sessions_closed}` |
| `network_get` | — | `{kind}` |
| `storage_changed` | — | `{executor_notified}` |

### Watching

| method | request | response |
|---|---|---|
| `playback_begin` | `series, episode` | `{playback_session, watch}` |
| `playback_prepare` | `playback_session, series, episode, criteria?, prefer_local? (true), session_id?, online_id?` | local `{mode:"local", item_id, item, target{kind:file\|content_uri, value}, other_local_copies, resume}` or online `{mode:"online", session_id, source, matches_criteria, criteria, variant, variants, variant_changed, other_local_copies, resume}`; `resume {position_ms, duration_ms, watched, watched_source}` |
| `stream_refresh` | `session_id, playback_session, variant?` | online fields |
| `playback_allow_cellular` | `playback_session` | `{allowed}` |
| `playback_position` | `playback_session, seq ≥ 1, position_ms, duration_ms?` | `{accepted}` |
| `playback_ended` | `playback_session, seq, position_ms, duration_ms` | `{accepted, watch}` |
| `playback_close` | `playback_session` | `{closed}` |
| `watch_set_watched` | `series_id, number, watched, expected_revision` | `{watch}` |
| `watch_get` | `series_id, numbers?` | `{episodes: [watch]}` |
| `watch_series` | `series_id` | `{last_opened, episodes}` |
| `watch_recent` | `limit?` (50, ≤ 500) | `{series: [{series, watch}]}` |
| `watch_open_in_browser` | `series, episode, session_id?, online_id?` | `{target: source\|episode_page, url, watch}` |
| `episode_next` | `series_id, current, episodes?` (numbers in the known order) | `{basis: listing\|none, next{number, local_copy}\|null, later_local_copy{number, item}\|null}` |

Client obligation: after `playback.consent_required`, stop new player transfers, let the buffer play and do not call
`stream_refresh` until `playback_allow_cellular`.

### Queue

| method | request | response |
|---|---|---|
| `queue_preview` | `series, episodes, selection{mode: episodes\|range\|unwatched, numbers?, range?}, criteria?, alternatives?, cellular_allowed?` | `{plan{series, episodes, criteria, alternatives, selection, cellular_allowed, basis_seq}, skipped[{number, reason}]}` |
| `queue_enqueue` | `plan` (from the preview; episodes may be removed) | `{added: [job], skipped: [{number, reason: already_queued\|already_downloaded\|changed_since_preview}]}` |
| `queue_snapshot` | `states?` | `{queue_revision, jobs, executor{state, stuck_jobs}}` |
| `queue_job` | `job_id` | `{job}` |
| `queue_pause`, `queue_resume`, `queue_cancel`, `queue_retry`, `queue_retry_interaction` | `job_id, expected_revision` | `{job}` |
| `queue_allow_cellular` | `job_id, allowed, expected_revision` | `{job}` |
| `queue_job_sources` | `job_id` | `{session_id, sources: [player + {matches_criteria, identity}], partial{source, variant}\|null}` |
| `queue_choose_source` | `job_id, session_id, online_id, variant?, restart?, expected_revision` | `{job}` |
| `queue_reorder` | `job_ids` (every open job, new order), `expected_revision` (queue revision) | `{queue_revision}` |

### Library (needs `storage`)

| method | request | response |
|---|---|---|
| `library_list` | `series_id?, status?` | `{items}` |
| `library_item` | `item_id` | `{item, watch}` |
| `library_verify` | `item_ids?, deep?` | batch |
| `library_delete` | `item_ids` | batch |
| `library_retry_deletes` | — | batch |
| `library_export` | `item_ids, target{kind: directory, path}\|{kind: platform, handle}, on_conflict? (fail\|rename)` | batch; not idempotent |

### Executor

| method | request | response |
|---|---|---|
| `executor_start` | — | `{state, already_running, recovery{counts, ids, orphans}\|null}` |
| `executor_status` | — | `{state: stopped\|running\|closing, stuck_jobs, owner: self\|other\|unknown}`; takes no lock: `other` is another runtime of this process, an owner in another process shows as `unknown` (and `executor_start` answers `executor.busy`) |
| `executor_stop` | `timeout_ms?` (10000) | `{ended, state, stuck_jobs}` |
| `executor_set_access` | `access` | `{applied_to_running_executor}` |

### Events

| method | request | response |
|---|---|---|
| `events_head` | — | `{epoch, seq}` |
| `events_next` | `cursor{epoch, seq}, max? (100), wait_ms? (0)` | `{epoch, events, next_seq, resync}` |
| `events_status` | — | `{epoch, oldest_seq, head_seq, listener_errors, dropped}` |

Event: `{epoch, seq, type, at, op_id|null, data}`. Entity signals (each with a revision): `job.changed {job_id,
revision, state, wait_reason, bytes_done, bytes_total}`, `queue.changed {queue_revision}`, `library.changed {item_id,
revision, status}`, `library.removed {item_id, last_revision}`, `preferences.changed {revision}`. Notices:
`executor.state {state}`, `executor.attempt_stuck {job_id}`, `executor.store_unavailable {operation}`,
`session.closed {session_id, reason}`, `network.changed {kind}`, `playback.consent_required {playback_session}`,
`engine.phase {phase, state}`, `engine.clearance {state}`, `engine.host {host, change}`,
`engine.source_failed {player, episode}`; push only: `events.resync_required {reason}` (`seq` null).

Resynchronising: `events_head`, then the snapshots (`queue_snapshot`, `library_list`, `preferences_get`,
`executor_status`), then `events_next` from the head; ignore a signal whose revision is not newer than the one held;
start over on another `epoch` or `resync: true`.
