"""Process state of one database file: local stores, file store, network, consents, events and the executor."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alt_ani_bridge.errors import BridgeError
from alt_ani_bridge.events import EventSink, Journal, PushAdapter, Snapshot, Watcher
from alt_ani_core.config import AccessConfig, XhrBackendName
from alt_ani_core.history.local import LocalState
from alt_ani_core.rules.network import CellularConsent
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.offline import OfflineQueue, StaticNetwork
from alt_ani_core.services.offline.library import LibraryService
from alt_ani_core.services.playback import PlaybackService
from alt_ani_core.state import EpisodeKey, NetworkKind, PlaybackSession
from alt_ani_core.storage.api import FileStore


@dataclass(frozen=True)
class RuntimeConfig:
    """``storage``, ``cache_dir`` and ``access`` belong to the runtime's owner; a joining handle must match them."""

    db_path: Path
    cache_dir: Path
    storage: tuple[str, str] | None
    access: AccessConfig
    network: NetworkKind
    xhr_backend: XhrBackendName
    antibot_delay_sec: float


class Runtime:
    def __init__(
        self,
        config: RuntimeConfig,
        state: LocalState,
        store: FileStore | None,
        *,
        journal_size: int,
        watch_interval: float,
        on_release: Callable[[], None] = lambda: None,
    ) -> None:
        self.config = config
        self.state = state
        self.store = store
        self.network = StaticNetwork(config.network)
        self.journal = Journal(journal_size)
        self.sink = EventSink(self.journal)
        self.watcher = Watcher(self._snapshot, self.journal, watch_interval)
        self.playback = PlaybackService(state, store)
        self.lock = threading.RLock()
        self.executor_access = config.access
        self.queue: OfflineQueue | None = None
        self.executor_engine: Engine | None = None
        self.closing = False
        self.released = False
        self.engine_ids: set[str] = set()
        # push adapters of closed handles whose delivery thread has not ended yet; under the bridge lock
        self.ending_pushes: list[PushAdapter] = []
        self._open_playbacks: dict[str, EpisodeKey] = {}
        self._consents: set[str] = set()
        self._on_release = on_release

    def _snapshot(self) -> Snapshot:
        queue_revision, jobs = self.state.queue.revisions()
        return Snapshot(queue_revision, jobs, self.state.library.revisions(), self.state.preferences.get().revision)

    def require_store(self) -> FileStore:
        if self.store is None:
            raise BridgeError("storage.not_configured", "this runtime was created without a file store")
        return self.store

    def library(self) -> LibraryService:
        return LibraryService(self.state.library, self.require_store())

    def changed(self) -> None:
        """After an in-process mutation: the executor re-reads the queue and the watcher looks now."""
        queue = self.queue
        if queue is not None:
            queue.wake()
        self.watcher.poke()

    def begin_playback(self, session: PlaybackSession) -> None:
        """P3: a new viewing ends consent and tracking of every other session, of this episode or another."""
        with self.lock:
            self._open_playbacks = {session.session_id: session.key}
            self._consents.clear()

    def end_playback(self, session_id: str) -> None:
        with self.lock:
            self._open_playbacks.pop(session_id, None)
            self._consents.discard(session_id)

    def allow_cellular(self, session_id: str, key: EpisodeKey) -> bool:
        """Only for the open viewing it names; False when that session is not the open one."""
        with self.lock:
            if self._open_playbacks.get(session_id) != key:
                return False
            self._consents.add(session_id)
            return True

    def consent(self, session_id: str | None) -> CellularConsent | None:
        with self.lock:
            key = self._open_playbacks.get(session_id or "")
            if key is None or session_id not in self._consents:
                return None
            return CellularConsent(key)

    def sessions_without_consent(self) -> list[tuple[str, EpisodeKey]]:
        with self.lock:
            return [(sid, key) for sid, key in self._open_playbacks.items() if sid not in self._consents]

    def snapshot_for_tests(self) -> dict[str, Any]:
        """Everything the runtime itself keeps in memory, for checks that no secret is held here."""
        with self.lock:
            return {
                "open_playbacks": dict(self._open_playbacks),
                "consents": sorted(self._consents),
                "executor_access": self.executor_access,
                "config": self.config,
            }

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._on_release()
