"""Refresh signals, not a change log: a numbered ring per runtime, a revision watcher and per-listener push threads.

A published event is stored as JSON text, so nothing can change it after its ``seq`` is assigned. Intermediate states
may be skipped; a client resynchronises with head -> snapshots -> cursor and ignores signals not newer than it knows.
"""

import contextvars
import json
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.events import (
    ClearanceRequested,
    ClearanceUnavailable,
    EmbedResolved,
    EngineEvent,
    ExtractorFallback,
    HostDeferred,
    HostDropped,
    HostLastResort,
    PhaseFinished,
    PhaseStarted,
    SourceAttemptFailed,
    XhrFallback,
)
from alt_ani_core.history.database import STORE_ERRORS
from alt_ani_core.services.offline.events import AttemptStuck, InteractionRequired, JobProgress, OrphanFound, StoreUnavailable
from alt_ani_core.state import ItemStatus, JobSignal

CURRENT_OP: contextvars.ContextVar[str | None] = contextvars.ContextVar("alt_ani_bridge_op", default=None)

_POLL_SLICE_SEC = 0.05

# engine events with a revision-backed or secret-bearing twin; deliberately not forwarded, not counted as dropped
_SUPPRESSED: tuple[type[EngineEvent], ...] = (
    JobProgress,
    InteractionRequired,
    OrphanFound,
    EmbedResolved,
    ExtractorFallback,
    XhrFallback,
)
_HOST_CHANGES: dict[type[EngineEvent], str] = {HostDeferred: "deferred", HostLastResort: "last_resort", HostDropped: "dropped"}


def notification(event: EngineEvent) -> tuple[str, dict[str, Any]] | None:
    """The allowlisted fields of an engine event; None for anything not forwarded."""
    if isinstance(event, PhaseStarted | PhaseFinished):
        return "engine.phase", {"phase": event.phase, "state": "started" if isinstance(event, PhaseStarted) else "finished"}
    if isinstance(event, ClearanceRequested | ClearanceUnavailable):
        return "engine.clearance", {"state": "requested" if isinstance(event, ClearanceRequested) else "unavailable"}
    if isinstance(event, HostDeferred | HostLastResort | HostDropped):
        return "engine.host", {"host": event.host, "change": _HOST_CHANGES[type(event)]}
    if isinstance(event, SourceAttemptFailed):
        return "engine.source_failed", {"player": event.player, "episode": event.episode}
    if isinstance(event, AttemptStuck):
        return "executor.attempt_stuck", {"job_id": event.job_id}
    if isinstance(event, StoreUnavailable):
        return "executor.store_unavailable", {"operation": event.operation}
    return None


class Journal:
    def __init__(self, size: int) -> None:
        self.epoch = uuid.uuid4().hex
        self._cond = threading.Condition()
        self._ring: deque[tuple[int, str]] = deque(maxlen=size)
        self._next = 1
        self.dropped = 0

    def publish(self, type_: str, data: dict[str, Any], *, op_id: str | None = None) -> int:
        with self._cond:
            seq = self._next
            text = json.dumps(
                {
                    "epoch": self.epoch,
                    "seq": seq,
                    "type": type_,
                    "at": datetime.now(UTC).isoformat(),
                    "op_id": op_id,
                    "data": data,
                }
            )
            self._ring.append((seq, text))
            self._next += 1
            self._cond.notify_all()
            return seq

    def count_dropped(self) -> None:
        with self._cond:
            self.dropped += 1

    def head(self) -> int:
        with self._cond:
            return self._next

    def oldest(self) -> int:
        with self._cond:
            return self._ring[0][0] if self._ring else self._next

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def read(self, cursor: int, limit: int, wait_sec: float, cancel: CancelToken = NEVER) -> tuple[list[dict], int, bool]:
        """Events from ``cursor``; ``resync`` when the cursor is behind the ring or ahead of the head."""
        remaining = wait_sec
        with self._cond:
            while True:
                cancel.raise_if_cancelled()
                oldest = self._ring[0][0] if self._ring else self._next
                if cursor < oldest or cursor > self._next:
                    return [], self._next, True
                if cursor < self._next:
                    first = cursor - oldest
                    picked = [json.loads(text) for _, text in list(self._ring)[first : first + limit]]
                    return picked, picked[-1]["seq"] + 1, False
                if remaining <= 0:
                    return [], cursor, False
                slice_sec = min(remaining, _POLL_SLICE_SEC)
                self._cond.wait(slice_sec)
                remaining -= slice_sec


class EventSink:
    """What the engine and executor emit into: allowlisted, tagged with the calling operation's ``op_id``."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    def emit(self, event: EngineEvent) -> None:
        mapped = notification(event)
        if mapped is None:
            if not isinstance(event, _SUPPRESSED):
                self._journal.count_dropped()
            return
        self._journal.publish(*mapped, op_id=CURRENT_OP.get())


@dataclass(frozen=True)
class Snapshot:
    queue_revision: int
    jobs: dict[str, JobSignal]
    items: dict[str, tuple[ItemStatus, int]]
    preferences_revision: int


class Watcher:
    """The only source of entity events; reads revisions only while someone listens, and never writes."""

    def __init__(self, read: Callable[[], Snapshot], journal: Journal, interval: float) -> None:
        self._read = read
        self._journal = journal
        self._interval = interval
        self._cond = threading.Condition()
        self._pass_lock = threading.Lock()
        self._listeners = 0
        self._poked = False
        self._stopped = False
        self._base: Snapshot | None = None
        self._thread: threading.Thread | None = None
        self.passes = 0
        self.read_errors = 0

    def attach(self) -> None:
        with self._cond:
            self._listeners += 1
            if self._thread is None and not self._stopped:
                self._thread = threading.Thread(target=self._loop, name="bridge-watcher", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def detach(self) -> None:
        with self._cond:
            self._listeners -= 1

    def poke(self) -> None:
        with self._cond:
            self._poked = True
            self._cond.notify_all()

    def stop(self, timeout: float | None) -> bool:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def sync(self) -> None:
        """One pass now; the base it leaves is never newer than what it published."""
        with self._pass_lock:
            try:
                current = self._read()
            except STORE_ERRORS:
                self.read_errors += 1
                return
            self.passes += 1
            if self._base is not None:
                self._publish(self._base, current)
            self._base = current

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._listeners <= 0 and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
            self.sync()
            with self._cond:
                if not self._poked and not self._stopped:
                    self._cond.wait(self._interval)
                self._poked = False

    def _publish(self, old: Snapshot, new: Snapshot) -> None:
        publish = self._journal.publish
        for job_id, signal in new.jobs.items():
            before = old.jobs.get(job_id)
            if before is None or before.revision != signal.revision:
                publish(
                    "job.changed",
                    {
                        "job_id": job_id,
                        "revision": signal.revision,
                        "state": signal.state.value,
                        "wait_reason": signal.wait_reason.value if signal.wait_reason else None,
                        "bytes_done": signal.bytes_done,
                        "bytes_total": signal.bytes_total,
                    },
                )
        if new.queue_revision != old.queue_revision:
            publish("queue.changed", {"queue_revision": new.queue_revision})
        for item_id, (status, revision) in new.items.items():
            before_item = old.items.get(item_id)
            if before_item is None or before_item[1] != revision or before_item[0] is not status:
                publish("library.changed", {"item_id": item_id, "revision": revision, "status": status.value})
        for item_id in old.items.keys() - new.items.keys():
            publish("library.removed", {"item_id": item_id, "last_revision": old.items[item_id][1]})
        if new.preferences_revision != old.preferences_revision:
            publish("preferences.changed", {"revision": new.preferences_revision})


class PushAdapter:
    """Delivers the journal to one host listener on its own thread; a slow or failing listener hurts only itself."""

    def __init__(self, journal: Journal, watcher: Watcher, listener: Any, batch: int = 100) -> None:
        self._journal = journal
        self._watcher = watcher
        self._deliver_to = getattr(listener, "onEvent", None) or listener
        self._batch = batch
        self._stop = CancelToken()
        self.errors = 0
        self.resyncs = 0
        self._cursor = journal.head()
        watcher.attach()
        self.thread = threading.Thread(target=self._run, name="bridge-push", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.cancelled:
                try:
                    events, cursor, resync = self._journal.read(self._cursor, self._batch, 0.5, self._stop)
                except BaseException:
                    return
                if resync:
                    self.resyncs += 1
                    self._send(
                        {
                            "epoch": self._journal.epoch,
                            "seq": None,
                            "type": "events.resync_required",
                            "at": None,
                            "op_id": None,
                            "data": {"reason": "overflow"},
                        }
                    )
                    self._cursor = self._journal.head()
                    continue
                for event in events:
                    if self._stop.cancelled:
                        return
                    self._send(event)
                self._cursor = cursor
        finally:
            self._watcher.detach()

    def _send(self, event: dict) -> None:
        try:
            self._deliver_to(json.dumps(event))
        except Exception:
            self.errors += 1

    def stop(self, timeout: float | None) -> bool:
        """True once the delivery thread has ended; a listener stuck in its callback keeps it alive."""
        self._stop.cancel()
        self._journal.wake()
        if self.thread is threading.current_thread():
            return False
        self.thread.join(timeout)
        return not self.thread.is_alive()
