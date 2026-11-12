"""Registries of engines, episode sessions and running operations behind a JSON-in/JSON-out call interface."""

import dataclasses
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alt_ani_bridge import dto
from alt_ani_bridge.errors import BridgeError, describe
from alt_ani_core import diagnostics
from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import DEFAULT_ANTIBOT_DELAY_SEC, AccessConfig, EngineConfig
from alt_ani_core.errors import EngineInterrupt
from alt_ani_core.events import EngineEvent
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.history.store import HistoryStore
from alt_ani_core.models import FallbackPolicy, PlayerEntry, Purpose, SourceFilter
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.transport.api import Transport

Payload = dict[str, Any]


def _default_transport() -> Transport:
    try:
        from alt_ani_core.transport.curl import CurlTransport
    except ImportError:
        raise BridgeError("transport_unavailable", "no HTTP transport is available on this platform") from None
    return CurlTransport()


class _ListenerSink:
    """A failing host callback must never break an engine operation: its errors are swallowed and counted."""

    def __init__(self, listener: Any) -> None:
        self._listener = listener
        self._lock = threading.Lock()
        self.errors = 0

    def emit(self, event: EngineEvent) -> None:
        if self._listener is None:
            return
        message = json.dumps({"type": type(event).__name__, **dataclasses.asdict(event)})
        try:
            deliver = getattr(self._listener, "onEvent", None) or self._listener
            deliver(message)
        except Exception:
            with self._lock:
                self.errors += 1


@dataclass
class _SessionEntry:
    """``closed`` and ``operations`` change only under the bridge lock, so closing and finishing an operation never race."""

    session_id: str
    session: EpisodeSession
    operations: set[str] = field(default_factory=set)
    closed_by: str | None = None


@dataclass
class _EngineEntry:
    engine: Engine
    sink: _ListenerSink
    sessions: dict[str, _SessionEntry] = field(default_factory=dict)
    operations: set[str] = field(default_factory=set)


@dataclass
class _Operation:
    """Every call is one, with or without a client ``op_id``; it is bound to the episode session it looks up."""

    key: str
    engine_id: str
    token: CancelToken
    session: _SessionEntry | None = None


class Bridge:
    """``call(method, json)`` always returns a JSON envelope; ``cancel(op_id)`` may be called from any thread."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], Transport] = _default_transport,
        history_factory: Callable[[Path], HistoryStore] = SqliteHistory,
    ) -> None:
        self._transport_factory = transport_factory
        self._history_factory = history_factory
        self._lock = threading.RLock()
        self._engines: dict[str, _EngineEntry] = {}
        self._operations: dict[str, _Operation] = {}
        self._client_ops: dict[str, str] = {}
        self._methods: dict[str, Callable[[Payload, _Operation], Any]] = {
            "set_access": self._set_access,
            "engine_status": self._engine_status,
            "search": self._search,
            "metadata": self._metadata,
            "metadata_many": self._metadata_many,
            "episodes": self._episodes,
            "open_episode": self._open_episode,
            "select": self._select,
            "resolve": self._resolve,
            "probe_hosts": self._probe_hosts,
            "reload_episode": self._reload_episode,
            "close_session": self._close_session,
            "history_watch": self._history_watch,
            "history_downloads": self._history_downloads,
            "history_record_watch": self._history_record_watch,
            "history_record_download": self._history_record_download,
            "history_clear": self._history_clear,
        }

    def create_engine(self, payload_json: str, listener: Any = None) -> str:
        """``listener`` gets each engine event as a JSON string via ``onEvent`` (or by being called)."""
        try:
            payload = _parse(payload_json)
            config = EngineConfig(
                cache_dir=Path(payload["cache_dir"]),
                xhr_backend=payload.get("xhr_backend", "new"),
                antibot_delay_sec=float(payload.get("antibot_delay_sec", DEFAULT_ANTIBOT_DELAY_SEC)),
                access=_access(payload.get("access") or {}),
            )
            if payload.get("diagnostics_dir"):
                diagnostics.configure(Path(payload["diagnostics_dir"]))
            sink = _ListenerSink(listener)
            engine = Engine(
                config,
                transport=self._transport_factory(),
                history=self._history_factory(Path(payload["history_path"])),
                events=sink,
            )
            engine_id = uuid.uuid4().hex
            with self._lock:
                self._engines[engine_id] = _EngineEntry(engine=engine, sink=sink)
            return _ok({"engine_id": engine_id})
        except (Exception, EngineInterrupt) as exc:
            return _fail(exc)

    def close_engine(self, payload_json: str) -> str:
        """Cancels the engine's running operations, drops its sessions and closes it; later use reports unknown_engine."""
        try:
            engine_id = str(_parse(payload_json)["engine_id"])
            with self._lock:
                entry = self._engines.pop(engine_id, None)
                if entry is None:
                    raise BridgeError("unknown_engine", "no such engine", engine_id=engine_id)
                for session in entry.sessions.values():
                    session.closed_by = "close_engine"
                tokens = [self._operations[key].token for key in entry.operations if key in self._operations]
                for token in tokens:
                    token.cancel()
            entry.engine.close()
            return _ok({"cancelled_operations": len(tokens), "closed_sessions": len(entry.sessions)})
        except (Exception, EngineInterrupt) as exc:
            return _fail(exc)

    def cancel(self, op_id: str) -> bool:
        """False for an id that is unknown or already finished; the operation itself reports ``cancelled``."""
        with self._lock:
            operation = self._operations.get(self._client_ops.get(op_id, ""))
            if operation is None:
                return False
            operation.token.cancel()
        return True

    def call(self, method: str, payload_json: str) -> str:
        """A result is success only if its episode session is still open when handed back, else ``session_closed``."""
        operation: _Operation | None = None
        client_id: str | None = None
        try:
            handler = self._methods.get(method)
            if handler is None:
                raise BridgeError("unknown_method", "no such method", method=method)
            payload = _parse(payload_json)
            raw_op_id = payload.get("op_id")
            with self._lock:
                entry = self._entry(payload)
                if raw_op_id is not None:
                    if str(raw_op_id) in self._client_ops:
                        raise BridgeError("operation_id_in_use", "an operation with this id is still running")
                    client_id = str(raw_op_id)
                operation = _Operation(key=uuid.uuid4().hex, engine_id=str(payload["engine_id"]), token=CancelToken())
                self._operations[operation.key] = operation
                entry.operations.add(operation.key)
                if client_id is not None:
                    self._client_ops[client_id] = operation.key
            result = handler(payload, operation)
            # one critical section with unregistering, so a close sees the operation running or not at all
            with self._lock:
                closed = _session_closed(operation)
                self._unregister(operation, client_id)
            return _fail(closed) if closed is not None else _ok(result)
        except (Exception, EngineInterrupt) as exc:
            if operation is not None:
                with self._lock:
                    closed = _session_closed(operation)
                    self._unregister(operation, client_id)
                return _fail(closed or exc)
            return _fail(exc)
        finally:
            if operation is not None:
                self._unregister(operation, client_id)

    def _unregister(self, operation: _Operation, client_id: str | None) -> None:
        with self._lock:
            self._operations.pop(operation.key, None)
            if client_id is not None and self._client_ops.get(client_id) == operation.key:
                del self._client_ops[client_id]
            if (entry := self._engines.get(operation.engine_id)) is not None:
                entry.operations.discard(operation.key)
            if operation.session is not None:
                operation.session.operations.discard(operation.key)

    def _entry(self, payload: Payload) -> _EngineEntry:
        engine_id = str(payload["engine_id"])
        with self._lock:
            entry = self._engines.get(engine_id)
        if entry is None:
            raise BridgeError("unknown_engine", "no such engine", engine_id=engine_id)
        return entry

    def _session(self, payload: Payload, operation: _Operation) -> EpisodeSession:
        """Binds ``operation`` to the session in the same critical section that finds it open."""
        session_id = str(payload["session_id"])
        with self._lock:
            found = self._entry(payload).sessions.get(session_id)
            if found is None:
                raise BridgeError("unknown_session", "no such episode session", session_id=session_id)
            operation.session = found
            found.operations.add(operation.key)
        return found.session

    def _close_locked(self, session: _SessionEntry, reason: str) -> int:
        session.closed_by = reason
        tokens = [self._operations[key].token for key in session.operations if key in self._operations]
        for token in tokens:
            token.cancel()
        return len(tokens)

    def _set_access(self, payload: Payload, _operation: _Operation) -> Any:
        entry = self._entry(payload)
        entry.engine.set_access(_access(payload["access"]))
        # sessions opened under the old access are stale and can never be used again
        with self._lock:
            dropped = list(entry.sessions.values())
            entry.sessions.clear()
            for session in dropped:
                self._close_locked(session, "set_access")
        return {"dropped_sessions": len(dropped)}

    def _engine_status(self, payload: Payload, operation: _Operation) -> Any:
        entry = self._entry(payload)
        with self._lock:
            return {
                "sessions": len(entry.sessions),
                "running_operations": len(entry.operations - {operation.key}),
                "listener_errors": entry.sink.errors,
            }

    def _search(self, payload: Payload, operation: _Operation) -> Any:
        hits = self._entry(payload).engine.search(str(payload["query"]), cancel=operation.token)
        return {"hits": [dto.series_hit(hit) for hit in hits]}

    def _metadata(self, payload: Payload, operation: _Operation) -> Any:
        engine = self._entry(payload).engine
        ref = engine.series_from_url(str(payload["url"]))
        return {"metadata": dto.series_metadata(engine.metadata(ref, cancel=operation.token))}

    def _metadata_many(self, payload: Payload, operation: _Operation) -> Any:
        engine = self._entry(payload).engine
        refs = [engine.series_from_url(str(url)) for url in payload["urls"]]
        return {"metadata": [dto.series_metadata(meta) for meta in engine.metadata_many(refs, cancel=operation.token)]}

    def _episodes(self, payload: Payload, operation: _Operation) -> Any:
        engine = self._entry(payload).engine
        ref = dto.parse_series_ref(payload["series"]) if "series" in payload else engine.series_from_url(str(payload["url"]))
        return dto.episode_listing(engine.episodes(ref, cancel=operation.token))

    def _open_episode(self, payload: Payload, operation: _Operation) -> Any:
        entry = self._entry(payload)
        session = entry.engine.open_episode(dto.parse_episode(payload["episode"]), cancel=operation.token)
        session_id = uuid.uuid4().hex
        with self._lock:
            if self._engines.get(str(payload["engine_id"])) is not entry:
                raise BridgeError("unknown_engine", "the engine was closed while the episode was opening")
            entry.sessions[session_id] = _SessionEntry(session_id=session_id, session=session)
        return {"session_id": session_id, "episode": dto.episode(session.episode), "sources": _sources(session)}

    def _select(self, payload: Payload, operation: _Operation) -> Any:
        flt = payload.get("filter") or {}
        selection = self._session(payload, operation).select(
            SourceFilter(lang=flt.get("lang"), subs=flt.get("subs"), player_name=flt.get("player_name")),
            purpose=Purpose(payload.get("purpose", Purpose.WATCH.value)),
        )
        return dto.selection(selection)

    def _resolve(self, payload: Payload, operation: _Operation) -> Any:
        session = self._session(payload, operation)
        by_id = {p.online_id: p for p in session.sources}
        chosen = _player(by_id, payload["online_id"])
        candidates = [_player(by_id, online_id) for online_id in payload.get("candidate_ids", [])]
        policy = FallbackPolicy(payload.get("policy", FallbackPolicy.SINGLE.value))
        if payload.get("rank_by_health"):
            candidates = session.rank(candidates)
        resolved = session.resolve(
            chosen,
            candidates=candidates,
            policy=policy,
            force_fresh=bool(payload.get("force_fresh", False)),
            cancel=operation.token,
        )
        return dto.resolved_stream(resolved, payload.get("quality"))

    def _probe_hosts(self, payload: Payload, operation: _Operation) -> Any:
        session = self._session(payload, operation)
        by_id = {p.online_id: p for p in session.sources}
        candidates = [_player(by_id, online_id) for online_id in payload.get("candidate_ids", list(by_id))]
        stop = session.probe_hosts(candidates, cancel=operation.token)
        return {
            "known_sources": [dto.known_source(source) for source in session.known_sources.values()],
            "stopped": describe(stop) if stop is not None else None,
        }

    def _reload_episode(self, payload: Payload, operation: _Operation) -> Any:
        session = self._session(payload, operation)
        session.reload(cancel=operation.token)
        return {"sources": _sources(session)}

    def _close_session(self, payload: Payload, _operation: _Operation) -> Any:
        """Operations still running on the session are cancelled and can no longer report success."""
        entry = self._entry(payload)
        with self._lock:
            session = entry.sessions.pop(str(payload["session_id"]), None)
            if session is None:
                return {"closed": False, "cancelled_operations": 0}
            return {"closed": True, "cancelled_operations": self._close_locked(session, "close_session")}

    def _history_watch(self, payload: Payload, _operation: _Operation) -> Any:
        history = self._entry(payload).engine.history
        return {"entries": [dto.watch_entry(entry) for entry in history.watch_entries()]}

    def _history_downloads(self, payload: Payload, _operation: _Operation) -> Any:
        history = self._entry(payload).engine.history
        return {"entries": [dto.download_entry(entry) for entry in history.download_entries()]}

    def _history_record_watch(self, payload: Payload, _operation: _Operation) -> Any:
        self._entry(payload).engine.history.record_watch(dto.parse_series_ref(payload["series"]), float(payload["episode"]))
        return {}

    def _history_record_download(self, payload: Payload, _operation: _Operation) -> Any:
        history = self._entry(payload).engine.history
        history.record_download(dto.parse_series_ref(payload["series"]), float(payload["episode"]))
        return {}

    def _history_clear(self, payload: Payload, _operation: _Operation) -> Any:
        self._entry(payload).engine.history.clear()
        return {}


def _parse(payload_json: str) -> Payload:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("the payload must be a JSON object")
    return payload


def _access(data: Payload) -> AccessConfig:
    return AccessConfig(
        flaresolverr_url=data.get("flaresolverr_url") or None,
        use_flaresolverr=bool(data.get("use_flaresolverr", True)),
    )


def _session_closed(operation: _Operation) -> BridgeError | None:
    session = operation.session
    if session is None or session.closed_by is None:
        return None
    return BridgeError(
        "session_closed",
        "the episode session was closed while the operation was running",
        session_id=session.session_id,
        closed_by=session.closed_by,
    )


def _player(by_id: dict[str, PlayerEntry], online_id: object) -> PlayerEntry:
    player = by_id.get(str(online_id))
    if player is None:
        raise BridgeError("unknown_source", "no such source in this episode session", online_id=str(online_id))
    return player


def _sources(session: EpisodeSession) -> list[dict[str, Any]]:
    return [dto.player(p) for p in session.sources]


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data})


def _fail(exc: BaseException) -> str:
    return json.dumps({"ok": False, "error": describe(exc)})
