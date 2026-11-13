"""Registries' entries and the per-call context handed to every method."""

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from alt_ani_bridge.errors import BridgeError
from alt_ani_bridge.events import CURRENT_OP, PushAdapter
from alt_ani_bridge.runtime import Runtime
from alt_ani_bridge.schema import Reader
from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.models import EpisodeRow
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.services.playback import PlaybackSelection
from alt_ani_core.transport.api import SessionProfile, Transport


class LazyTransport:
    """The platform transport is created on the first remote request, so local state works without one."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self._inner: Transport | None = None
        self._lock = threading.Lock()

    @property
    def started(self) -> bool:
        with self._lock:
            return self._inner is not None

    def open(self, profile: SessionProfile) -> Any:
        with self._lock:
            if self._inner is None:
                self._inner = self._factory()
            inner = self._inner
        return inner.open(profile)


@dataclass
class SessionEntry:
    """``closed_by`` and ``operations`` change only under the bridge lock, so closing and finishing never race."""

    session_id: str
    session: EpisodeSession
    episode: EpisodeRow
    operations: set[str] = field(default_factory=set)
    closed_by: str | None = None
    # the playback choice a stream refresh keeps; identities and criteria only, never a URL
    selection: PlaybackSelection | None = None
    playback_session_id: str | None = None


@dataclass
class EngineEntry:
    engine_id: str
    engine: Engine
    runtime: Runtime
    access: AccessConfig
    transport: LazyTransport
    push: PushAdapter | None = None
    sessions: dict[str, SessionEntry] = field(default_factory=dict)
    operations: set[str] = field(default_factory=set)


@dataclass
class Operation:
    """Every call is one, with or without a client ``op_id``; it is bound to the episode session it looks up."""

    key: str
    engine_id: str | None
    token: CancelToken
    client_id: str | None = None
    session: SessionEntry | None = None
    platform_handles: list[str] = field(default_factory=list)


class BridgeInternals(Protocol):
    lock: threading.RLock
    platform: Any

    def engine_entry(self, engine_id: str) -> EngineEntry: ...

    def engine_entries(self) -> list[EngineEntry]: ...

    def ensure_queue(self, runtime: Runtime) -> Any: ...

    def close_session_locked(self, session: SessionEntry, reason: str) -> int: ...


class Call:
    def __init__(self, bridge: BridgeInternals, reader: Reader, operation: Operation, entry: EngineEntry | None) -> None:
        self.bridge = bridge
        self.r = reader
        self.operation = operation
        self._entry = entry

    @property
    def entry(self) -> EngineEntry:
        if self._entry is None:
            raise BridgeError("unknown_engine", "this method needs an engine handle")
        return self._entry

    @property
    def runtime(self) -> Runtime:
        return self.entry.runtime

    @property
    def engine(self) -> Engine:
        return self.entry.engine

    @property
    def token(self) -> CancelToken:
        return self.operation.token

    def publish(self, type_: str, data: dict[str, Any]) -> None:
        self.runtime.journal.publish(type_, data, op_id=CURRENT_OP.get())

    def session(self, session_id: str) -> SessionEntry:
        """Binds the operation to the session in the same critical section that finds it open."""
        with self.bridge.lock:
            found = self.bridge.engine_entry(self.entry.engine_id).sessions.get(session_id)
            if found is None:
                raise BridgeError("unknown_session", "no such episode session", session_id=session_id)
            self.operation.session = found
            found.operations.add(self.operation.key)
        return found

    def open_session(self, episode: EpisodeRow) -> SessionEntry:
        entry = self.entry
        session = entry.engine.open_episode(episode, cancel=self.token)
        found = SessionEntry(session_id=uuid.uuid4().hex, session=session, episode=episode)
        with self.bridge.lock:
            if entry.runtime.closing or self.bridge.engine_entry(entry.engine_id) is not entry:
                raise BridgeError("unknown_engine", "the engine was closed while the episode was opening")
            entry.sessions[found.session_id] = found
            self.operation.session = found
            found.operations.add(self.operation.key)
        return found

    def drop_sessions(self, reason: str) -> int:
        with self.bridge.lock:
            dropped = list(self.entry.sessions.values())
            self.entry.sessions.clear()
            for session in dropped:
                self.bridge.close_session_locked(session, reason)
        for session in dropped:
            self.publish("session.closed", {"session_id": session.session_id, "reason": reason})
        return len(dropped)
