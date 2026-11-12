"""Persistent download queue: atomic user actions, scheduler primitives and attempt-fenced executor writes."""

import sqlite3
import uuid
from collections.abc import Sequence

from alt_ani_core.errors import StateConflictError, UnknownRecordError
from alt_ani_core.history import codec
from alt_ani_core.history.codec import Row
from alt_ani_core.history.database import Database, upsert_episode
from alt_ani_core.models import EpisodeRow, SeriesRef
from alt_ani_core.rules.job_state import check_transition, invalid, merge_stop
from alt_ani_core.rules.network import download_allowed
from alt_ani_core.state import (
    ACTIVE_STATES,
    AlternativesPolicy,
    AttemptStatus,
    Claim,
    DownloadCriteria,
    DownloadJob,
    DownloadNetwork,
    EnqueuePlan,
    EnqueueResult,
    EpisodeKey,
    ItemStatus,
    JobError,
    JobState,
    NetworkKind,
    OfflineItem,
    OfflineItemSpec,
    PartialTransfer,
    Recovery,
    SelectionMode,
    SkipReason,
    SourceIdentity,
    StopTarget,
    StorageLocator,
    VariantIdentity,
    WaitReason,
)

_QUEUE_REVISION = "queue_revision"
_OPEN = "state NOT IN ('completed','cancelled')"
_ACTIVE = "('running','stopping','finalizing')"
INTERRUPTED = "job.interrupted"


def _job(row: Row) -> DownloadJob:
    return DownloadJob(
        id=row["id"],
        key=codec.key_of(row),
        seq=row["seq"],
        queue_pos=row["queue_pos"],
        state=JobState(row["state"]),
        criteria=codec.criteria_of(row["crit_audio"], row["crit_subs"], row["crit_quality"]),
        alternatives=AlternativesPolicy(row["alternatives"]),
        selection_mode=SelectionMode(row["selection_mode"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        wait_reason=WaitReason(row["wait_reason"]) if row["wait_reason"] else None,
        stop_target=StopTarget(row["stop_target"]) if row["stop_target"] else None,
        stop_wait_reason=WaitReason(row["stop_wait_reason"]) if row["stop_wait_reason"] else None,
        source=codec.source_of(row["source_json"]),
        variant=codec.variant_of(row["variant_json"]),
        cellular_allowed=bool(row["cellular_allowed"]),
        attempt_id=row["attempt_id"],
        attempts=row["attempts"],
        bytes_done=row["bytes_done"],
        bytes_total=row["bytes_total"],
        partial=codec.partial_of(row),
        target=codec.locator_of(row["target_backend"], row["target_key"]),
        last_error=codec.error_of(row),
        offline_item_id=row["offline_item_id"],
        completed_at=row["completed_at"],
        revision=row["revision"],
    )


def _unknown(job_id: str) -> UnknownRecordError:
    return UnknownRecordError(f"no download job {job_id}").coded("state.unknown_job", job_id=job_id)


class SqliteDownloadQueue:
    """The only source of truth about the queue; platform workers read and change it through these operations."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _set(self, conn: sqlite3.Connection, job_id: str, **columns: object) -> None:
        sets = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(
            f"UPDATE download_job SET {sets}, updated_at = ?, revision = revision + 1 WHERE id = ?",  # nosec B608
            (*columns.values(), self._db.now(), job_id),
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, job_id: str) -> DownloadJob:
        row = conn.execute("SELECT * FROM download_job WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise _unknown(job_id)
        return _job(row)

    @staticmethod
    def _by_attempt(conn: sqlite3.Connection, attempt_id: str) -> DownloadJob | None:
        row = conn.execute("SELECT * FROM download_job WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return None if row is None else _job(row)

    @staticmethod
    def _queue_revision(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (_QUEUE_REVISION,)).fetchone()
        return int(row[0]) if row else 0

    def _bump_queue(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_QUEUE_REVISION, str(self._queue_revision(conn) + 1)),
        )

    @staticmethod
    def _skip_reason(conn: sqlite3.Connection, key: EpisodeKey) -> SkipReason | None:
        """A legacy download record is not proof of a file, and a missing item is not one either."""
        args = (key.series_id, key.number)
        open_job = "SELECT 1 FROM download_job WHERE series_id = ? AND number = ? AND state NOT IN ('completed','cancelled')"
        if conn.execute(open_job, args).fetchone():
            return SkipReason.ALREADY_QUEUED
        available = "SELECT 1 FROM offline_item WHERE series_id = ? AND number = ? AND status = 'available'"
        if conn.execute(available, args).fetchone():
            return SkipReason.ALREADY_DOWNLOADED
        return None

    def _plan(self, conn: sqlite3.Connection, keys: Sequence[EpisodeKey]) -> EnqueuePlan:
        to_add: list[EpisodeKey] = []
        skipped: list[tuple[EpisodeKey, SkipReason]] = []
        for key in dict.fromkeys(keys):
            reason = self._skip_reason(conn, key)
            if reason is None:
                to_add.append(key)
            else:
                skipped.append((key, reason))
        return EnqueuePlan(tuple(to_add), tuple(skipped))

    def preview(self, series_id: str, numbers: Sequence[float]) -> EnqueuePlan:
        """Read-only; ``enqueue`` checks again in its own transaction, so a stale preview cannot add a duplicate."""
        with self._db.read() as conn:
            return self._plan(conn, [EpisodeKey(series_id, n) for n in numbers])

    def enqueue(
        self,
        series: SeriesRef,
        episodes: Sequence[EpisodeRow],
        criteria: DownloadCriteria,
        *,
        alternatives: AlternativesPolicy = AlternativesPolicy.OFF,
        selection_mode: SelectionMode = SelectionMode.AUTO,
        source: SourceIdentity | None = None,
        variant: VariantIdentity | None = None,
        cellular_allowed: bool = False,
    ) -> EnqueueResult:
        if selection_mode is SelectionMode.MANUAL and source is None:
            raise ValueError("a manual selection needs the chosen source")
        rows = {EpisodeKey(series.id, ep.number): ep for ep in episodes}
        added: list[str] = []
        with self._db.write() as conn:
            now = self._db.now()
            plan = self._plan(conn, list(rows))
            next_seq, next_pos = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1, COALESCE(MAX(queue_pos), 0) + 1 FROM download_job"
            ).fetchone()
            for offset, key in enumerate(plan.to_add):
                upsert_episode(conn, series, rows[key], now)
                job_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO download_job (id, seq, series_id, number, queue_pos, state, crit_audio, crit_subs,
                         crit_quality, alternatives, selection_mode, source_json, variant_json, cellular_allowed,
                         created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        next_seq + offset,
                        key.series_id,
                        key.number,
                        next_pos + offset,
                        criteria.audio,
                        criteria.subs,
                        criteria.quality.encode(),
                        AlternativesPolicy(alternatives).value,
                        SelectionMode(selection_mode).value,
                        codec.source_json(source),
                        codec.variant_json(variant),
                        int(cellular_allowed),
                        now,
                        now,
                    ),
                )
                added.append(job_id)
            if added:
                self._bump_queue(conn)
            jobs = tuple(self._row(conn, job_id) for job_id in added)
        return EnqueueResult(jobs, plan.skipped)

    def get(self, job_id: str) -> DownloadJob:
        with self._db.read() as conn:
            return self._row(conn, job_id)

    def jobs(self, states: Sequence[JobState] | None = None) -> list[DownloadJob]:
        with self._db.read() as conn:
            rows = conn.execute("SELECT * FROM download_job ORDER BY queue_pos, seq").fetchall()
        jobs = [_job(r) for r in rows]
        return jobs if states is None else [j for j in jobs if j.state in states]

    def queue_revision(self) -> int:
        with self._db.read() as conn:
            return self._queue_revision(conn)

    def reorder(self, job_ids: Sequence[str], expected_revision: int) -> int:
        """``job_ids`` must be exactly the open jobs the caller saw, in the new order; returns the new revision."""
        with self._db.write() as conn:
            revision = self._queue_revision(conn)
            open_ids = {r[0] for r in conn.execute(f"SELECT id FROM download_job WHERE {_OPEN}")}  # nosec B608
            if revision != expected_revision or len(job_ids) != len(open_ids) or set(job_ids) != open_ids:
                raise StateConflictError("the queue changed since it was read").coded(
                    "state.conflict", expected=expected_revision, found=revision
                )
            for pos, job_id in enumerate(job_ids, start=1):
                self._set(conn, job_id, queue_pos=pos)
            self._bump_queue(conn)
            return revision + 1

    def _stop(self, conn: sqlite3.Connection, job: DownloadJob, target: StopTarget, reason: WaitReason | None) -> bool:
        """Records a stop request on a job whose attempt still runs; the attempt keeps its slot until acknowledged."""
        merged = target if job.stop_target is None else merge_stop(job.stop_target, target)
        if merged is None or (merged is StopTarget.WAITING and job.stop_target is StopTarget.WAITING):
            # an earlier wait reason is kept: an interaction still pending must not turn into a network wait
            return False
        self._set(
            conn,
            job.id,
            state=JobState.STOPPING.value,
            stop_target=merged.value,
            stop_wait_reason=reason.value if merged is StopTarget.WAITING and reason else None,
        )
        return True

    def _user_change(self, job_id: str, target: StopTarget) -> DownloadJob:
        with self._db.write() as conn:
            job = self._row(conn, job_id)
            direct = {StopTarget.PAUSED: JobState.PAUSED, StopTarget.CANCELLED: JobState.CANCELLED}
            if job.state is direct.get(target):
                return job
            if target is StopTarget.QUEUED and job.state is JobState.RUNNING:
                raise invalid(job.id, job.state, JobState.QUEUED.value)
            if job.state in (JobState.RUNNING, JobState.STOPPING):
                if not self._stop(conn, job, target, None):
                    raise invalid(job.id, job.state, target.value)
            elif target is StopTarget.QUEUED:
                if job.state is not JobState.PAUSED:
                    raise invalid(job.id, job.state, JobState.QUEUED.value)
                self._set(conn, job.id, state=JobState.QUEUED.value)
            else:
                check_transition(job.id, job.state, direct[target])
                self._set(conn, job.id, state=direct[target].value, wait_reason=None)
            return self._row(conn, job_id)

    def pause(self, job_id: str) -> DownloadJob:
        """A running job becomes ``stopping``: the row change alone does not stop its transfer."""
        return self._user_change(job_id, StopTarget.PAUSED)

    def resume(self, job_id: str) -> DownloadJob:
        """Undoes a manual pause only; a network or storage wait is lifted by the scheduler, not by resume."""
        return self._user_change(job_id, StopTarget.QUEUED)

    def cancel(self, job_id: str) -> DownloadJob:
        """Keeps the partial's locator so its file can be cleaned up; an offline item already made is untouched."""
        return self._user_change(job_id, StopTarget.CANCELLED)

    def retry(self, job_id: str) -> DownloadJob:
        with self._db.write() as conn:
            job = self._row(conn, job_id)
            if job.state is not JobState.FAILED:
                raise invalid(job.id, job.state, "retry")
            self._set(conn, job.id, state=JobState.QUEUED.value)
            return self._row(conn, job_id)

    def allow_cellular(self, job_id: str, allowed: bool) -> DownloadJob:
        """Consent for this job only; other jobs and later episodes do not inherit it."""
        with self._db.write() as conn:
            job = self._row(conn, job_id)
            if job.state in (JobState.COMPLETED, JobState.CANCELLED):
                raise invalid(job.id, job.state, "allow_cellular")
            self._set(conn, job.id, cellular_allowed=int(allowed))
            return self._row(conn, job_id)

    def choose_source(
        self, job_id: str, source: SourceIdentity, variant: VariantIdentity | None = None, *, restart: bool = False
    ) -> DownloadJob:
        """A user decision; another source or variant always drops the partial, since fragments are never spliced."""
        with self._db.write() as conn:
            job = self._row(conn, job_id)
            if job.state in ACTIVE_STATES or job.state in (JobState.COMPLETED, JobState.CANCELLED):
                raise invalid(job.id, job.state, "choose_source")
            partial = job.partial
            keep = not restart and partial is not None and partial.source == source and partial.variant == variant
            columns: dict[str, object] = {
                "source_json": codec.source_json(source),
                "variant_json": codec.variant_json(variant),
                "selection_mode": SelectionMode.MANUAL.value,
            }
            if not keep:
                # the file itself is the executor's to remove; an unreferenced partial is an orphan for reconciliation
                for column in ("partial_backend", "partial_key", "partial_identity_json", "partial_validator_json"):
                    columns[column] = None
                columns |= {"bytes_done": None, "bytes_total": None}
            if job.state is JobState.WAITING:
                columns |= {"state": JobState.QUEUED.value, "wait_reason": None}
            self._set(conn, job.id, **columns)
            return self._row(conn, job_id)

    def hold(self, job_id: str, reason: WaitReason) -> bool:
        """Scheduler primitive: False when the job is paused, cancelled, already waiting or otherwise not holdable."""
        with self._db.write() as conn:
            job = self._row(conn, job_id)
            if job.state is JobState.QUEUED:
                self._set(conn, job.id, state=JobState.WAITING.value, wait_reason=reason.value)
                return True
            if job.state in (JobState.RUNNING, JobState.STOPPING):
                return self._stop(conn, job, StopTarget.WAITING, reason)
            return False

    def release(self, job_id: str, reason: WaitReason) -> bool:
        """Scheduler primitive: lifts only a wait for ``reason``; a manual pause is never lifted here."""
        with self._db.write() as conn:
            job = self._row(conn, job_id)
            if job.state is JobState.WAITING and job.wait_reason is reason:
                self._set(conn, job.id, state=JobState.QUEUED.value, wait_reason=None)
                return True
            if job.state is JobState.STOPPING and job.stop_target is StopTarget.WAITING and job.stop_wait_reason is reason:
                self._set(conn, job.id, stop_target=StopTarget.QUEUED.value, stop_wait_reason=None)
                return True
            return False

    def claim_next(self, network: NetworkKind) -> Claim | None:
        """Never exceeds the active limit (stopping and finalizing attempts count) nor the job's network permission."""
        with self._db.write() as conn:
            prefs = conn.execute("SELECT download_network, max_active_downloads FROM preferences").fetchone()
            active = conn.execute(f"SELECT COUNT(*) FROM download_job WHERE state IN {_ACTIVE}").fetchone()[0]  # nosec B608
            if active >= prefs["max_active_downloads"]:
                return None
            policy = DownloadNetwork(prefs["download_network"])
            rows = conn.execute("SELECT * FROM download_job WHERE state = 'queued' ORDER BY queue_pos, seq")
            job = next((j for j in map(_job, rows) if download_allowed(network, policy, j.cellular_allowed)), None)
            if job is None:
                return None
            attempt_id = uuid.uuid4().hex
            self._set(
                conn,
                job.id,
                state=JobState.RUNNING.value,
                attempt_id=attempt_id,
                attempt_started_at=self._db.now(),
                attempts=job.attempts + 1,
            )
            return Claim(self._row(conn, job.id), attempt_id)

    @staticmethod
    def _status(job: DownloadJob | None) -> AttemptStatus:
        if job is None:
            return AttemptStatus.LOST
        return AttemptStatus.STOP if job.state is JobState.STOPPING else AttemptStatus.CONTINUE

    def report_progress(self, attempt_id: str, bytes_done: int, bytes_total: int | None = None) -> AttemptStatus:
        """Polled from the transfer's progress callback: STOP means end the I/O and acknowledge, LOST means abandon."""
        if bytes_done < 0 or (bytes_total is not None and bytes_total < 0):
            raise ValueError("byte counts cannot be negative")
        with self._db.write() as conn:
            job = self._by_attempt(conn, attempt_id)
            if job is not None and job.state in (JobState.RUNNING, JobState.STOPPING):
                self._set(conn, job.id, bytes_done=bytes_done, bytes_total=bytes_total, progress_at=self._db.now())
            return self._status(job)

    def set_partial(self, attempt_id: str, partial: PartialTransfer, bytes_done: int | None = None) -> AttemptStatus:
        backend, key, identity, validator = codec.partial_columns(partial)
        with self._db.write() as conn:
            job = self._by_attempt(conn, attempt_id)
            if job is not None and job.state in (JobState.RUNNING, JobState.STOPPING):
                self._set(
                    conn,
                    job.id,
                    partial_backend=backend,
                    partial_key=key,
                    partial_identity_json=identity,
                    partial_validator_json=validator,
                    bytes_done=bytes_done if bytes_done is not None else job.bytes_done,
                )
            return self._status(job)

    def set_target(self, attempt_id: str, locator: StorageLocator) -> AttemptStatus:
        """Recorded before the transfer writes there, so recovery can find a file published right before a crash."""
        with self._db.write() as conn:
            job = self._by_attempt(conn, attempt_id)
            if job is not None and job.state in (JobState.RUNNING, JobState.STOPPING):
                self._set(conn, job.id, target_backend=locator.backend, target_key=locator.key)
            return self._status(job)

    def _settle(self, conn: sqlite3.Connection, job: DownloadJob, error: JobError | None = None) -> None:
        """Applies a stopping job's target once its attempt has ended."""
        if job.stop_target is None:
            raise invalid(job.id, job.state, "settle")
        code, params = codec.error_columns(error)
        waiting = job.stop_target is StopTarget.WAITING and job.stop_wait_reason is not None
        columns: dict[str, object] = {
            "state": job.stop_target.value,
            "wait_reason": job.stop_wait_reason.value if waiting and job.stop_wait_reason else None,
            "stop_target": None,
            "stop_wait_reason": None,
            "attempt_id": None,
        }
        if error is not None:
            columns |= {"last_error_code": code, "last_error_params": params, "last_error_at": self._db.now()}
        self._set(conn, job.id, **columns)

    def ack_stopped(self, attempt_id: str) -> bool:
        """The executor's confirmation that its I/O has ended; only now the job reaches its requested state."""
        with self._db.write() as conn:
            job = self._by_attempt(conn, attempt_id)
            if job is None or job.state is not JobState.STOPPING:
                return False
            self._settle(conn, job)
            return True

    def begin_finalize(self, attempt_id: str) -> bool:
        """The only permission to publish the file; refused once a stop was requested or the attempt was lost."""
        with self._db.write() as conn:
            job = self._by_attempt(conn, attempt_id)
            if job is None or job.state is not JobState.RUNNING:
                return False
            self._set(conn, job.id, state=JobState.FINALIZING.value)
            return True

    def complete(self, attempt_id: str, spec: OfflineItemSpec) -> OfflineItem | None:
        """One transaction creates the offline item and completes the job; the file must already be in place."""
        with self._db.write() as conn:
            job = self._by_attempt(conn, attempt_id)
            if job is None or job.state is not JobState.FINALIZING:
                return None
            now = self._db.now()
            item_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO offline_item (id, series_id, number, storage_backend, storage_key, status, size_bytes,
                     audio, subs, height, variant_label, protocol, container, source_player, created_at, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    job.key.series_id,
                    job.key.number,
                    spec.locator.backend,
                    spec.locator.key,
                    ItemStatus.AVAILABLE.value,
                    spec.size_bytes,
                    spec.audio,
                    spec.subs,
                    spec.height,
                    spec.variant_label,
                    spec.protocol,
                    spec.container,
                    spec.source_player,
                    now,
                    now,
                ),
            )
            self._set(
                conn,
                job.id,
                state=JobState.COMPLETED.value,
                attempt_id=None,
                completed_at=now,
                offline_item_id=item_id,
            )
            row = conn.execute("SELECT * FROM offline_item WHERE id = ?", (item_id,)).fetchone()
            return codec.item_of(row)

    def _end_attempt(self, attempt_id: str, state: JobState, error: JobError | None, wait: WaitReason | None) -> bool:
        with self._db.write() as conn:
            job = self._by_attempt(conn, attempt_id)
            if job is None:
                return False
            if job.state is JobState.STOPPING:
                # the attempt ended on its own, but what the user or scheduler asked for still wins
                self._settle(conn, job, error)
                return True
            check_transition(job.id, job.state, state)
            code, params = codec.error_columns(error)
            self._set(
                conn,
                job.id,
                state=state.value,
                wait_reason=wait.value if wait else None,
                attempt_id=None,
                last_error_code=code,
                last_error_params=params,
                last_error_at=self._db.now() if error else None,
            )
            return True

    def wait(self, attempt_id: str, reason: WaitReason, error: JobError | None = None) -> bool:
        """The executor stopped by itself and needs a condition: network, interaction, storage or a source decision."""
        return self._end_attempt(attempt_id, JobState.WAITING, error, reason)

    def fail(self, attempt_id: str, error: JobError) -> bool:
        return self._end_attempt(attempt_id, JobState.FAILED, error, None)

    def recover_interrupted(self) -> Recovery:
        """Call once per process before any executor starts; assumes one executing process per database file."""
        requeued: list[str] = []
        settled: list[str] = []
        reconcile: list[Claim] = []
        with self._db.write() as conn:
            rows = conn.execute(f"SELECT * FROM download_job WHERE state IN {_ACTIVE} ORDER BY seq").fetchall()  # nosec B608
            for job in map(_job, rows):
                if job.state is JobState.STOPPING:
                    self._settle(conn, job)
                    settled.append(job.id)
                elif job.state is JobState.RUNNING:
                    code, params = codec.error_columns(JobError(INTERRUPTED))
                    self._set(
                        conn,
                        job.id,
                        state=JobState.QUEUED.value,
                        attempt_id=None,
                        last_error_code=code,
                        last_error_params=params,
                        last_error_at=self._db.now(),
                    )
                    requeued.append(job.id)
                else:
                    # the old attempt is void; the reconciler gets a fresh one to check the target and complete or fail
                    attempt_id = uuid.uuid4().hex
                    self._set(conn, job.id, attempt_id=attempt_id)
                    reconcile.append(Claim(self._row(conn, job.id), attempt_id))
        return Recovery(tuple(requeued), tuple(settled), tuple(reconcile))
