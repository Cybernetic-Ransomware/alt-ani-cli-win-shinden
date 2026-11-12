"""One attempt of one job on its own thread; it ends in exactly one store operation, only after its I/O ended."""

import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from alt_ani_core.cancel import CancelToken
from alt_ani_core.errors import (
    NO_SPACE,
    TARGET_EXISTS,
    EngineInterrupt,
    StateConflictError,
    StorageError,
    UnknownRecordError,
)
from alt_ani_core.events import EventSink
from alt_ani_core.history.database import STORE_ERRORS
from alt_ani_core.history.local import LocalState
from alt_ani_core.models import PlayerEntry, SeriesRef, StreamVariant
from alt_ani_core.rules import retry
from alt_ani_core.rules.criteria import NoMatch, match_source, match_variant, same_player, source_identity, variant_identity
from alt_ani_core.rules.network import download_allowed
from alt_ani_core.rules.resume import (
    ResumeStep,
    complete_evidence,
    evidence_of,
    may_discard_partial,
    plan_resume,
    with_checkpoint,
)
from alt_ani_core.services.offline.events import InteractionRequired, JobProgress, StoreUnavailable
from alt_ani_core.services.offline.interaction import Grants, InteractionNeeded
from alt_ani_core.services.offline.network import NetworkSource
from alt_ani_core.services.offline.sources import EpisodeSources, SourceProvider, SourceUnavailable
from alt_ani_core.state import (
    AttemptStatus,
    Claim,
    DownloadJob,
    JobError,
    OfflineItemSpec,
    PartialTransfer,
    ResourceValidator,
    SelectionMode,
    SourceIdentity,
    StorageLocator,
    VariantIdentity,
    WaitReason,
)
from alt_ani_core.storage.api import FileStore, PublishName
from alt_ani_core.storage.fs import fsync_file, sha256_path
from alt_ani_core.transfer import CheckpointPolicy, ResumeRefused, TransferCallbacks, TransferFailed, TransferResult, transfer

_EXPIRED_STATUSES = {403, 404, 410}
_MEDIA_SKIP_SUFFIXES = (".part", ".ytdl", ".json", ".tmp")
_RESERVE_TRIES = 5


@dataclass
class ExecutorConfig:
    progress_interval_sec: float = 2.0
    checkpoint: CheckpointPolicy = field(default_factory=CheckpointPolicy)
    socket_timeout_sec: float = 15.0
    monitor_interval_sec: float = 0.25
    idle_interval_sec: float = 5.0
    unreadable_stop_sec: float = 10.0
    store_retries: int = 5
    store_retry_delay_sec: float = 0.5

    @property
    def stuck_after_sec(self) -> float:
        return 2 * self.socket_timeout_sec + 10


@dataclass
class Context:
    state: LocalState
    store: FileStore
    sources: SourceProvider
    network: NetworkSource
    grants: Grants
    events: EventSink
    config: ExecutorConfig
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = lambda: datetime.now(UTC)
    transfer: Callable[..., TransferResult] = transfer


class _Stopped(Exception):  # noqa: N818 - control flow inside the attempt
    pass


class _Finished(Exception):  # noqa: N818 - control flow inside the attempt
    pass


class Attempt:
    """Owned by the scheduler; ``stop`` only cancels the token, the attempt itself settles the job afterwards."""

    def __init__(self, ctx: Context, claim: Claim) -> None:
        self.ctx = ctx
        self.job: DownloadJob = claim.job
        self.attempt_id = claim.attempt_id
        self.token = CancelToken()
        self.lost = False
        self.stop_kind = "stop"
        self.partial: PartialTransfer | None = claim.job.partial
        self.validator: ResourceValidator | None = None
        self._last_report = float("-inf")
        self._finished = threading.Event()
        self._unsettled: Callable[[], Any] | None = None
        self.thread = threading.Thread(target=self.run, name=f"offline-attempt-{claim.job.id[:8]}", daemon=True)

    @property
    def io_ended(self) -> bool:
        return self._finished.is_set()

    @property
    def done(self) -> bool:
        """The I/O ended and the store holds the outcome; until then the scheduler keeps the attempt."""
        return self._finished.is_set() and self._unsettled is None

    def settle_pending(self) -> bool:
        """Retries the outcome write that failed after the I/O ended; True once nothing is left to store."""
        call = self._unsettled
        if not self._finished.is_set() or call is None:
            return self.done
        try:
            call()
        except STORE_ERRORS:
            return False
        except Exception:
            # the store refused rather than failed: recovery settles the job at the next start
            self._unsettled = None
            return True
        self._unsettled = None
        return True

    def stop(self, kind: str = "stop") -> None:
        if not self.token.cancelled:
            self.stop_kind = kind
        self.token.cancel()

    @property
    def queue(self):
        return self.ctx.state.queue

    def run(self) -> None:
        unexpected = False
        try:
            self._run()
        except _Finished:
            pass
        except _Stopped, EngineInterrupt:
            self._end_stopped()
        except BaseException:
            unexpected = True
        if unexpected:
            # nothing of the exception is stored: messages may carry URLs or paths
            with suppress(_Finished):
                self._finish(retry.fail("executor.unexpected"))
        self._finished.set()

    def _store_call(self, name: str, call: Callable[[], Any]) -> Any:
        """The outcome write must land: after bounded retries it is kept for the scheduler to retry (``settle_pending``)."""
        config = self.ctx.config
        for attempt in range(config.store_retries):
            try:
                return call()
            except STORE_ERRORS:
                if attempt == config.store_retries - 1:
                    self.ctx.events.emit(StoreUnavailable(operation=name))
                    self._unsettled = call
                    return None
                threading.Event().wait(config.store_retry_delay_sec)
        return None

    def _finish(self, outcome: retry.Outcome) -> NoReturn:
        queue = self.queue
        if outcome.action is retry.Action.WAIT and outcome.reason is not None:
            reason = outcome.reason
            self._store_call("wait", lambda: queue.wait(self.attempt_id, reason, outcome.error))
        elif outcome.action is retry.Action.DEFER:
            not_before = (self.ctx.wall() + timedelta(seconds=outcome.delay_sec)).isoformat()
            self._store_call("defer", lambda: queue.defer(self.attempt_id, outcome.error, not_before, counts=outcome.counts))
        else:
            self._store_call("fail", lambda: queue.fail(self.attempt_id, outcome.error))
        raise _Finished

    def _end_stopped(self) -> None:
        if self.lost:
            return
        queue, error = self.queue, JobError(f"executor.{self.stop_kind}")

        def settle() -> bool:
            # nothing in the store asked for this stop (shutdown, unreadable store): give the slot back unspent
            return queue.ack_stopped(self.attempt_id) or queue.defer(self.attempt_id, error, None, counts=False)

        self._store_call("ack_stopped", settle)

    def _check(self, status: AttemptStatus | None) -> None:
        if status is AttemptStatus.LOST:
            self.lost = True
            self.token.cancel()
            raise _Stopped
        if status is AttemptStatus.STOP:
            self.token.cancel()
            raise _Stopped
        if self.token.cancelled:
            raise _Stopped

    def _network_allowed(self) -> bool:
        try:
            job = self.queue.get(self.job.id)
            policy = self.ctx.state.preferences.get().download_network
        except STORE_ERRORS:
            return True
        return download_allowed(self.ctx.network.current(), policy, job.cellular_allowed)

    def _decide(self, code: str, /, **params: str | int | None) -> NoReturn:
        self._finish(retry.wait(WaitReason.SOURCE_DECISION, code, **params))

    def _run(self) -> None:
        job = self.job
        try:
            series, episode = self.queue.episode_of(job.key)
        except UnknownRecordError:
            self._finish(retry.fail("state.unknown_episode"))
        if self.partial is not None and evidence_of(self.partial.validator) is not None:
            if self._finalize_existing(series):
                return
            if not may_discard_partial(job):
                self._decide("resume.impossible", reason="evidence_mismatch")
            self.partial = None
        try:
            sources = self.ctx.sources.open(episode, grant=self.ctx.grants.get(job.id), cancel=self.token)
        except InteractionNeeded as need:
            self._interaction(need)
        except SourceUnavailable as unavailable:
            self._unavailable([unavailable])
        self._check(None)
        self._try_sources(sources, series)

    def _candidates(self, sources: EpisodeSources) -> list[PlayerEntry]:
        job = self.job
        entries = list(sources.entries)
        if job.selection_mode is SelectionMode.MANUAL and job.source is not None:
            wanted = job.source
            exact = [e for e in entries if source_identity(e) == wanted]
            return exact or [e for e in entries if same_player(source_identity(e), wanted)][:1]
        matching = [e for e in entries if match_source(e, job.criteria)]
        if self.partial is None:
            return matching
        first = [e for e in matching if source_identity(e) == self.partial.source]
        others = [e for e in matching if e not in first] if may_discard_partial(job) else []
        return first + others

    def _try_sources(self, sources: EpisodeSources, series: SeriesRef) -> None:
        job = self.job
        candidates = self._candidates(sources)
        if not candidates:
            if job.selection_mode is SelectionMode.MANUAL:
                self._decide("source.gone")
            if self.partial is not None:
                self._decide("resume.source_unavailable")
            self._decide("criteria.no_match")
        problems: list[Exception] = []
        for entry in candidates:
            self._check(None)
            try:
                stream = sources.resolve(entry, fresh=False, cancel=self.token)
            except (InteractionNeeded, SourceUnavailable) as exc:
                problems.append(exc)
                continue
            chosen = match_variant(stream, job.criteria.quality)
            if isinstance(chosen, NoMatch):
                continue
            self._download(sources, series, entry, chosen)
        if self.partial is not None and not may_discard_partial(job) and not problems:
            self._decide("resume.source_unavailable")
        self._unavailable(problems)

    def _unavailable(self, problems: list[Exception]) -> NoReturn:
        need = next((p for p in problems if isinstance(p, InteractionNeeded)), None)
        if need is not None:
            self._interaction(need)
        unavailable = [p for p in problems if isinstance(p, SourceUnavailable)]
        if not unavailable:
            self._decide("criteria.no_match")
        if any(u.transient for u in unavailable):
            after = max((u.retry_after or 0.0 for u in unavailable), default=0.0)
            self._finish(retry.transient(self.job.retry_count, "source.unavailable", retry_after=after or None))
        self._finish(retry.fail("source.exhausted"))

    def _interaction(self, need: InteractionNeeded) -> NoReturn:
        self.ctx.events.emit(InteractionRequired(job_id=self.job.id, scope=need.scope, host=need.host))
        self._finish(
            retry.wait(
                WaitReason.INTERACTION, "interaction.required", scope=need.scope, host=need.host, online_id=need.online_id
            )
        )

    def _download(self, sources: EpisodeSources, series: SeriesRef, entry: PlayerEntry, variant: StreamVariant) -> None:
        job = self.job
        sid, vid = source_identity(entry), variant_identity(variant)
        plan = plan_resume(self.partial, sid, vid)
        if plan.step is ResumeStep.IMPOSSIBLE and not may_discard_partial(job):
            self._decide("resume.impossible", reason=plan.reason)
        resume = plan.step is ResumeStep.RESUME
        refreshed = False
        refusals = 0
        while True:
            try:
                result = self._transfer(sid, vid, variant, resume=resume)
            except ResumeRefused as refused:
                self._check(None)
                refusals += 1
                if not may_discard_partial(job) or refusals > 1:
                    # a fresh transfer refused as well means the server keeps changing the resource under us
                    self._decide("resume.impossible", reason=refused.reason)
                resume = False
                continue
            except TransferFailed as failed:
                self._check(None)
                if failed.http_status in _EXPIRED_STATUSES and not refreshed:
                    # an expired stream URL: resolve the same source afresh and continue from the evidence
                    refreshed = True
                    try:
                        stream = sources.resolve(entry, fresh=True, cancel=self.token)
                    except (InteractionNeeded, SourceUnavailable) as exc:
                        self._unavailable([exc])
                    chosen = match_variant(stream, job.criteria.quality)
                    if isinstance(chosen, NoMatch) or variant_identity(chosen) != vid:
                        self._decide("resume.impossible", reason="variant_changed")
                    variant = chosen
                    replan = plan_resume(self.partial, sid, vid)
                    if replan.step is ResumeStep.IMPOSSIBLE and not may_discard_partial(job):
                        self._decide("resume.impossible", reason=replan.reason)
                    resume = replan.step is ResumeStep.RESUME
                    continue
                self._finish(
                    retry.transfer_outcome(
                        failed.category,
                        failed.http_status,
                        network_allowed=self._network_allowed(),
                        retry_count=job.retry_count,
                    )
                )
            except StorageError as exc:
                self._storage_failure(exc)
            self._finalize(series, result.media, sid, vid, result.extension)

    def _storage_failure(self, exc: StorageError) -> NoReturn:
        if exc.code == NO_SPACE:
            self._finish(retry.wait(WaitReason.STORAGE, NO_SPACE))
        self._finish(retry.fail(exc.code or "storage.failed"))

    def _set_partial(self, partial: PartialTransfer, bytes_done: int | None) -> None:
        status = self.queue.set_partial(self.attempt_id, partial, bytes_done)
        self.partial = partial
        if status is AttemptStatus.LOST:
            self.lost = True
            self.token.cancel()
        elif status is AttemptStatus.STOP:
            self.token.cancel()

    def _transfer(self, sid: SourceIdentity, vid: VariantIdentity, variant: StreamVariant, *, resume: bool) -> TransferResult:
        ctx = self.ctx
        if resume and self.partial is not None:
            locator = self.partial.locator
            self.validator = self.partial.validator
        else:
            locator = ctx.store.partial_locator(self.job.id, uuid.uuid4().hex)
            # referenced before any file appears, so a crash leaves nothing unaccounted for
            self._set_partial(PartialTransfer(locator, sid, vid, None), 0)
            self._check(None)
            self.validator = None
        work = ctx.store.work_path(locator)

        def on_validator(validator: ResourceValidator) -> None:
            self.validator = validator
            self._set_partial(PartialTransfer(locator, sid, vid, validator), 0)

        def on_checkpoint(length: int, fragments: int) -> None:
            if self.validator is None:
                return
            checkpointed = with_checkpoint(self.validator, length, fragments)
            try:
                self._set_partial(PartialTransfer(locator, sid, vid, checkpointed), length)
            except STORE_ERRORS:
                return
            self.validator = checkpointed

        callbacks = TransferCallbacks(validator=on_validator, progress=self._on_progress, checkpoint=on_checkpoint)
        return ctx.transfer(
            variant,
            work,
            resume=self.validator if resume else None,
            callbacks=callbacks,
            policy=ctx.config.checkpoint,
            events=ctx.events,
            cancel=self.token,
            socket_timeout=ctx.config.socket_timeout_sec,
        )

    def _on_progress(self, done: int, total: int | None) -> None:
        now = self.ctx.clock()
        if now - self._last_report < self.ctx.config.progress_interval_sec:
            return
        self._last_report = now
        self.ctx.events.emit(JobProgress(job_id=self.job.id, bytes_done=done, bytes_total=total))
        try:
            status = self.queue.report_progress(self.attempt_id, done, total)
        except STORE_ERRORS:
            return
        if status is AttemptStatus.LOST:
            self.lost = True
            self.token.cancel()
        elif status is AttemptStatus.STOP:
            self.token.cancel()

    def _media_in(self, work: Path) -> Path | None:
        files = [
            p
            for p in work.glob("media.*")
            if p.is_file() and not p.name.endswith(_MEDIA_SKIP_SUFFIXES) and "-Frag" not in p.name
        ]
        return files[0] if len(files) == 1 else None

    def _finalize_existing(self, series: SeriesRef) -> bool:
        """A partial already proven complete (e.g. a full disk while publishing): verify it again and publish."""
        partial = self.partial
        evidence = evidence_of(partial.validator) if partial else None
        if partial is None or evidence is None:
            return False
        try:
            media = self._media_in(self.ctx.store.work_path(partial.locator))
            if media is None or media.stat().st_size != evidence[0] or sha256_path(media, self.token) != evidence[1]:
                return False
        except OSError, StorageError:
            return False
        self._publish(series, media, partial.source, partial.variant, media.suffix.lstrip("."), evidence)
        return True

    def _finalize(self, series: SeriesRef, media: Path, sid: SourceIdentity, vid: VariantIdentity, ext: str) -> None:
        try:
            size = media.stat().st_size
            if size <= 0:
                self._finish(retry.fail("transfer.empty"))
            fsync_file(media)
            digest = sha256_path(media, self.token)
        except OSError:
            self._finish(retry.fail("transfer.missing_output"))
        evidence = complete_evidence(size, digest)
        if self.partial is None:
            self._finish(retry.fail("executor.unexpected"))
        self._set_partial(PartialTransfer(self.partial.locator, sid, vid, evidence), size)
        self._check(None)
        self._publish(series, media, sid, vid, ext, (size, digest))

    def _reserve(self, series: SeriesRef, ext: str) -> StorageLocator:
        name = PublishName(series.id, series.title, f"{self.job.key.number:g}", ext)
        taken: set[str] = set()
        for _ in range(_RESERVE_TRIES):
            locator = self.ctx.store.allocate(name, frozenset(taken))
            try:
                status = self.queue.set_target(self.attempt_id, locator)
            except StateConflictError:
                taken.add(locator.key)
                continue
            self._check(status)
            return locator
        self._finish(retry.fail("storage.location_conflict"))

    def _publish(
        self,
        series: SeriesRef,
        media: Path,
        sid: SourceIdentity,
        vid: VariantIdentity,
        ext: str,
        evidence: tuple[int, str],
    ) -> None:
        try:
            target = self._reserve(series, ext)
        except StorageError as exc:
            self._storage_failure(exc)
        if not self.queue.begin_finalize(self.attempt_id):
            self._check(self.queue.report_progress(self.attempt_id, evidence[0], evidence[0]))
            raise _Stopped
        try:
            self.ctx.store.publish(media, target)
        except StorageError as exc:
            if exc.code == TARGET_EXISTS:
                self._finish(retry.fail("storage.location_conflict"))
            self._storage_failure(exc)
        spec = OfflineItemSpec(
            locator=target,
            size_bytes=evidence[0],
            audio=sid.lang_audio or None,
            subs=sid.lang_subs or None,
            height=vid.height,
            variant_label=vid.label or None,
            protocol=vid.protocol,
            container=ext or None,
            source_player=sid.player or None,
            sha256=evidence[1],
        )
        queue = self.queue

        def complete() -> object:
            try:
                return queue.complete(self.attempt_id, spec)
            except StateConflictError:
                return queue.fail(self.attempt_id, retry.fail("storage.location_conflict").error)

        self._store_call("complete", complete)
        raise _Finished
