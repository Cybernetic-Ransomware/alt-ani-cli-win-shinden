"""The offline executor: the one owner process of a database runs its queue; others get ``executor.busy``."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.errors import ExecutorBusyError, InvalidTransitionError
from alt_ani_core.events import NULL_SINK, EventSink
from alt_ani_core.history.local import LocalState
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.models import EpisodeRow, SeriesRef
from alt_ani_core.services.offline.attempt import Context, ExecutorConfig
from alt_ani_core.services.offline.interaction import Grants, InteractionGrant
from alt_ani_core.services.offline.library import ItemResult, LibraryService
from alt_ani_core.services.offline.network import NetworkSource, StaticNetwork
from alt_ani_core.services.offline.recovery import RecoveryReport, recover
from alt_ani_core.services.offline.scheduler import Scheduler
from alt_ani_core.services.offline.sources import SourceProvider
from alt_ani_core.state import (
    DownloadCriteria,
    DownloadJob,
    EnqueueResult,
    Preferences,
    SourceIdentity,
    VariantIdentity,
    WaitReason,
)
from alt_ani_core.storage.api import ExportTarget, FileStore

__all__ = ["ExecutorConfig", "ExecutorState", "ExecutorStatus", "OfflineQueue", "StaticNetwork"]


class ExecutorState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    # a close timed out: some attempt's I/O still runs and ownership is kept until it ends
    CLOSING = "closing"


@dataclass(frozen=True)
class ExecutorStatus:
    state: ExecutorState
    stuck_jobs: tuple[str, ...] = ()


class OfflineQueue:
    def __init__(
        self,
        state: LocalState,
        store: FileStore,
        sources: SourceProvider,
        *,
        network: NetworkSource | None = None,
        events: EventSink = NULL_SINK,
        config: ExecutorConfig | None = None,
        **context: Any,
    ) -> None:
        self.state = state
        self.store = store
        self.library = LibraryService(state.library, store)
        self._grants = Grants()
        self._ctx = Context(
            state=state,
            store=store,
            sources=sources,
            network=network or StaticNetwork(),
            grants=self._grants,
            events=events,
            config=config or ExecutorConfig(),
            **context,
        )
        self._owner: ExecutorOwnership | None = None
        self._scheduler: Scheduler | None = None
        self._closing = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._scheduler is not None

    @property
    def sources(self) -> SourceProvider:
        return self._ctx.sources

    def status(self) -> ExecutorStatus:
        with self._lock:
            scheduler, closing = self._scheduler, self._closing
        if scheduler is None:
            return ExecutorStatus(ExecutorState.STOPPED)
        if not closing:
            return ExecutorStatus(ExecutorState.RUNNING)
        stuck = tuple(a.job.id for a in scheduler.active if not a.io_ended)
        return ExecutorStatus(ExecutorState.CLOSING, stuck)

    def start(self, *, cancel: CancelToken = NEVER) -> RecoveryReport:
        """Ownership first, then the root's guarantees, then recovery; only then may anything be claimed."""
        with self._lock:
            if self._closing:
                raise InvalidTransitionError("the executor is still closing").coded("executor.closing")
            if self._scheduler is not None:
                raise RuntimeError("the executor is already started")
            owner = ExecutorOwnership.try_acquire(self.state.database.path)
            if owner is None:
                raise ExecutorBusyError("another process executes this download queue").coded("executor.busy")
            try:
                self.store.check_root()
                report = recover(self.state, self.store, owner, self._ctx.events, cancel)
            except BaseException:
                owner.release()
                raise
            self._owner = owner
            self._scheduler = Scheduler(self._ctx, owner)
            self._scheduler.start()
            return report

    def close(self, timeout: float | None = None) -> bool:
        """Ownership is released only when the loop and every attempt's I/O have ended; on False, call it again to wait."""
        with self._lock:
            scheduler, owner = self._scheduler, self._owner
            if scheduler is not None:
                self._closing = True
        if scheduler is None or owner is None:
            return True
        if not scheduler.close(timeout):
            return False
        with self._lock:
            if self._scheduler is scheduler:
                self._scheduler, self._owner = None, None
            self._closing = False
        owner.release()
        return True

    def __enter__(self) -> OfflineQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def wake(self) -> None:
        if self._scheduler is not None:
            self._scheduler.wake()

    def _woken[T](self, value: T) -> T:
        self.wake()
        return value

    def enqueue(self, series: SeriesRef, episodes: Sequence[EpisodeRow], criteria: DownloadCriteria, **kwargs) -> EnqueueResult:
        return self._woken(self.state.queue.enqueue(series, episodes, criteria, **kwargs))

    def pause(self, job_id: str, expected_revision: int | None = None) -> DownloadJob:
        return self._woken(self.state.queue.pause(job_id, expected_revision))

    def resume(self, job_id: str, expected_revision: int | None = None) -> DownloadJob:
        return self._woken(self.state.queue.resume(job_id, expected_revision))

    def cancel(self, job_id: str, expected_revision: int | None = None) -> DownloadJob:
        return self._woken(self.state.queue.cancel(job_id, expected_revision))

    def retry(self, job_id: str, expected_revision: int | None = None) -> DownloadJob:
        return self._woken(self.state.queue.retry(job_id, expected_revision))

    def allow_cellular(self, job_id: str, allowed: bool, expected_revision: int | None = None) -> DownloadJob:
        return self._woken(self.state.queue.allow_cellular(job_id, allowed, expected_revision))

    def choose_source(
        self,
        job_id: str,
        source: SourceIdentity,
        variant: VariantIdentity | None = None,
        *,
        restart: bool = False,
        expected_revision: int | None = None,
    ) -> DownloadJob:
        return self._woken(
            self.state.queue.choose_source(job_id, source, variant, restart=restart, expected_revision=expected_revision)
        )

    def retry_interaction(self, job_id: str, expected_revision: int | None = None) -> DownloadJob:
        """Another attempt without a grant: the stored choice is kept and the job may come back waiting."""
        self._grants.drop(job_id)
        return self._woken(self.state.queue.retry_interaction(job_id, expected_revision))

    def reorder(self, job_ids: Sequence[str], expected_revision: int) -> int:
        return self._woken(self.state.queue.reorder(job_ids, expected_revision))

    def update_preferences(self, **changes: Any) -> Preferences:
        return self._woken(self.state.preferences.update(**changes))

    def complete_interaction(self, job_id: str, grant: InteractionGrant) -> bool:
        """The grant stays in memory for the next attempt; the job keeps the source it was waiting with."""
        self._grants.put(job_id, grant)
        return self._woken(self.state.queue.release(job_id, WaitReason.INTERACTION))

    def notify_storage_changed(self) -> None:
        if self._scheduler is not None:
            self._scheduler.notify_storage_changed()

    def delete(self, item_ids: list[str], *, cancel: CancelToken = NEVER) -> list[ItemResult]:
        return self._woken(self.library.delete(item_ids, cancel=cancel))

    def export(self, item_ids: list[str], target: ExportTarget | Path, **kwargs) -> list[ItemResult]:
        return self.library.export(item_ids, target, **kwargs)
