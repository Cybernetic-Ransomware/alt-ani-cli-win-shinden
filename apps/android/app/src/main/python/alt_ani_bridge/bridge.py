"""Registries of runtimes, engine handles, episode sessions and running operations behind ``call(method, json)``."""

import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from alt_ani_bridge import contract
from alt_ani_bridge.context import Call, EngineEntry, LazyTransport, Operation, SessionEntry
from alt_ani_bridge.errors import BridgeError, describe
from alt_ani_bridge.events import CURRENT_OP, PushAdapter
from alt_ani_bridge.methods import (
    Method,
    catalogue,
    common,
    events,
    executor,
    library,
    method,
    playback,
    queue,
    settings,
    watch,
)
from alt_ani_bridge.methods.settings import effective_access
from alt_ani_bridge.platform import FILE_STORE, PlatformObjects
from alt_ani_bridge.runtime import Runtime, RuntimeConfig
from alt_ani_bridge.schema import InputError, Reader, parse
from alt_ani_core import diagnostics
from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import DEFAULT_ANTIBOT_DELAY_SEC, AccessConfig, EngineConfig, XhrBackendName
from alt_ani_core.errors import EngineInterrupt, OperationCancelled
from alt_ani_core.history.local import LocalState, open_local_state
from alt_ani_core.history.store import HistoryStore
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.offline import OfflineQueue
from alt_ani_core.services.offline.sources import EngineSources, SourceProvider
from alt_ani_core.shinden.xhr import parse_backend
from alt_ani_core.state import NetworkKind
from alt_ani_core.storage.api import FileStore
from alt_ani_core.storage.fs import FsFileStore
from alt_ani_core.transport.api import Transport

Payload = dict[str, Any]

_CREATE_FIELDS = (
    "cache_dir",
    "history_path",
    "xhr_backend",
    "antibot_delay_sec",
    "access",
    "diagnostics_dir",
    "storage",
    "network",
    "contract",
)
_DELIVERY_JOIN_SEC = 2.0


def _default_transport() -> Transport:
    try:
        from alt_ani_core.transport.curl import CurlTransport
    except ImportError:
        raise BridgeError("transport_unavailable", "no HTTP transport is available on this platform") from None
    return CurlTransport()


def _check_contract(r: Reader) -> None:
    if not r.has("contract"):
        return
    requested = r.text("contract")
    if requested.split(".", 1)[0] != str(contract.MAJOR):
        raise BridgeError(
            "unsupported_contract", "unsupported contract major version", requested=requested, supported=contract.CONTRACT
        )


class Bridge:
    """``call(method, json)`` always returns a JSON envelope; ``cancel(op_id)`` may be called from any thread."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], Transport] = _default_transport,
        history_factory: Callable[[Path], HistoryStore] | None = None,
        state_factory: Callable[[Path], LocalState] = open_local_state,
        sources_factory: Callable[[Runtime], SourceProvider] | None = None,
        executor_options: dict[str, Any] | None = None,
        watch_interval: float = 0.5,
        journal_size: int = 2048,
    ) -> None:
        self._transport_factory = transport_factory
        self._history_factory = history_factory
        self._state_factory = state_factory
        self._sources_factory = sources_factory
        self._executor_options = dict(executor_options or {})
        self._watch_interval = watch_interval
        self._journal_size = journal_size
        self.lock = threading.RLock()
        self.platform = PlatformObjects()
        self._engines: dict[str, EngineEntry] = {}
        self._runtimes: dict[Path, Runtime] = {}
        self._operations: dict[str, Operation] = {}
        self._client_ops: dict[str, str] = {}
        # op_id -> expiry: a cancel that came before its call registered; the call ends ``cancelled`` without running
        self._early_cancels: OrderedDict[str, float] = OrderedDict()
        self._operation_ended = threading.Condition(self.lock)
        self._methods: dict[str, Method] = {
            "bridge_info": method(self._bridge_info, needs_engine=False),
            "runtime_close": method(self._runtime_close, "db_path", "timeout_ms", needs_engine=False),
        }
        for module in (catalogue, settings, watch, playback, queue, library, executor, events):
            self._methods.update(module.METHODS)

    def engine_entry(self, engine_id: str) -> EngineEntry:
        with self.lock:
            entry = self._engines.get(engine_id)
        if entry is None:
            raise BridgeError("unknown_engine", "no such engine", engine_id=engine_id)
        return entry

    def engine_entries(self) -> list[EngineEntry]:
        with self.lock:
            return list(self._engines.values())

    def close_session_locked(self, session: SessionEntry, reason: str) -> int:
        session.closed_by = reason
        tokens = [self._operations[key].token for key in session.operations if key in self._operations]
        for token in tokens:
            token.cancel()
        return len(tokens)

    def register_platform_object(self, obj: Any) -> str:
        """A host file store or export target; the handle is used in ``storage`` or an export ``target``."""
        try:
            handle, kind = self.platform.register(obj)
            return _ok({"handle": handle, "kind": kind})
        except (Exception, EngineInterrupt) as exc:
            return _fail(exc)

    def release_platform_object(self, handle: str) -> str:
        try:
            self.platform.release(str(handle))
            return _ok({"released": True})
        except (Exception, EngineInterrupt) as exc:
            return _fail(exc)

    def _storage(self, r: Reader) -> tuple[tuple[str, str] | None, FileStore | None]:
        s = r.obj("storage", ("kind", "root", "handle"), None)
        if s is None:
            return None, None
        if s.choice("kind", ("fs", "platform")) == "fs":
            root = Path(s.text("root")).resolve()
            return ("fs", str(root)), FsFileStore(root)
        handle = s.text("handle")
        return ("platform", handle), self.platform.acquire(handle, FILE_STORE, s.field("handle"))

    def _runtime_for(self, r: Reader) -> Runtime:
        """The first handle of a database fixes the runtime's file store, cache and executor access."""
        db_path = Path(r.text("history_path")).resolve()
        cache_dir = Path(r.text("cache_dir"))
        access = common.access(r, required=False)
        network = r.enum("network", NetworkKind, None)
        with self.lock:
            existing = self._runtimes.get(db_path)
            if existing is not None:
                if existing.closing:
                    raise BridgeError("runtime.closing", "the runtime of this database is closing", db_path=str(db_path))
                config = existing.config
                storage = r.obj("storage", ("kind", "root", "handle"), None)
                if storage is not None:
                    if storage.choice("kind", ("fs", "platform")) == "fs":
                        wanted = ("fs", str(Path(storage.text("root")).resolve()))
                    else:
                        wanted = ("platform", storage.text("handle"))
                    if wanted != config.storage:
                        raise BridgeError("runtime.config_conflict", "the runtime uses another file store", field="storage")
                if cache_dir.resolve() != config.cache_dir.resolve():
                    raise BridgeError("runtime.config_conflict", "the runtime uses another cache directory", field="cache_dir")
                if access is not None and access != config.access:
                    raise BridgeError("runtime.config_conflict", "the runtime uses another executor access", field="access")
                if network is not None and network is not existing.network.current():
                    raise BridgeError("runtime.config_conflict", "the runtime is on another network", field="network")
                return existing
            storage_key, store = self._storage(r)
            config = RuntimeConfig(
                db_path=db_path,
                cache_dir=cache_dir,
                storage=storage_key,
                access=access or AccessConfig(),
                network=network or NetworkKind.WIFI,
                xhr_backend=_backend(r),
                antibot_delay_sec=r.number("antibot_delay_sec", DEFAULT_ANTIBOT_DELAY_SEC, minimum=0),
            )
            handle = storage_key[1] if storage_key and storage_key[0] == "platform" else None
            runtime = Runtime(
                config,
                self._state_factory(db_path),
                store,
                journal_size=self._journal_size,
                watch_interval=self._watch_interval,
                on_release=(lambda: self.platform.release_use(handle)) if handle else (lambda: None),
            )
            self._runtimes[db_path] = runtime
            return runtime

    def create_engine(self, payload_json: str, listener: Any = None) -> str:
        """No HTTP transport is created here; ``listener`` gets each event as JSON on its own delivery thread."""
        try:
            r = Reader(parse(payload_json))
            r.only(_CREATE_FIELDS)
            _check_contract(r)
            runtime = self._runtime_for(r)
            access = common.access(r, required=False) or runtime.config.access
            config = EngineConfig(
                cache_dir=Path(r.text("cache_dir")),
                xhr_backend=_backend(r),
                antibot_delay_sec=r.number("antibot_delay_sec", DEFAULT_ANTIBOT_DELAY_SEC, minimum=0),
                access=effective_access(access, runtime.network.current()),
            )
            if r.has("diagnostics_dir"):
                diagnostics.configure(Path(r.text("diagnostics_dir")))
            transport = LazyTransport(self._transport_factory)
            history = self._history_factory(runtime.config.db_path) if self._history_factory else runtime.state.history
            engine = Engine(config, transport=transport, history=history, events=runtime.sink)
            engine_id = uuid.uuid4().hex
            entry = EngineEntry(engine_id=engine_id, engine=engine, runtime=runtime, access=access, transport=transport)
            # a runtime_close may have finished while the engine was being built; a late handle must not outlive it.
            # The delivery thread starts only once the handle is registered, so every close sees it in ending_pushes.
            with self.lock:
                current = self._runtimes.get(runtime.config.db_path) is runtime and not runtime.closing
                if current:
                    if listener is not None:
                        entry.push = PushAdapter(runtime.journal, runtime.watcher, listener)
                    self._engines[engine_id] = entry
                    runtime.engine_ids.add(engine_id)
            if not current:
                engine.close()
                raise BridgeError(
                    "runtime.closing", "the runtime of this database is closing", db_path=str(runtime.config.db_path)
                )
            return _ok({"engine_id": engine_id, "contract": contract.CONTRACT})
        except (Exception, EngineInterrupt) as exc:
            return _fail(exc)

    def _detach(self, entry: EngineEntry, exclude: str | None = None) -> tuple[int, int]:
        """Under the bridge lock: forgets the handle, cancels its operations and marks its sessions closed."""
        self._engines.pop(entry.engine_id, None)
        entry.runtime.engine_ids.discard(entry.engine_id)
        if entry.push is not None:
            entry.runtime.ending_pushes.append(entry.push)
        for session in entry.sessions.values():
            session.closed_by = "close_engine"
        tokens = [self._operations[k].token for k in entry.operations if k in self._operations and k != exclude]
        for token in tokens:
            token.cancel()
        return len(tokens), len(entry.sessions)

    def close_engine(self, payload_json: str) -> str:
        """Cancels the handle's operations, drops its sessions and push listener; the executor and runtime stay."""
        try:
            r = Reader(parse(payload_json))
            r.only(("engine_id", "contract"))
            engine_id = r.text("engine_id")
            with self.lock:
                entry = self._engines.get(engine_id)
                if entry is None:
                    raise BridgeError("unknown_engine", "no such engine", engine_id=engine_id)
                cancelled, closed = self._detach(entry)
            entry.engine.close()
            ended = self._stop_push(entry.runtime, entry.push, _DELIVERY_JOIN_SEC) if entry.push else True
            return _ok({"cancelled_operations": cancelled, "closed_sessions": closed, "delivery_ended": ended})
        except (Exception, EngineInterrupt) as exc:
            return _fail(exc)

    def cancel(self, op_id: str) -> bool:
        """True for a running operation; any other id is remembered so a call not registered yet ends ``cancelled``."""
        with self.lock:
            operation = self._operations.get(self._client_ops.get(op_id, ""))
            if operation is None:
                self._remember_cancel(op_id)
                return False
            operation.token.cancel()
        return True

    def _remember_cancel(self, op_id: str) -> None:
        now = time.monotonic()
        while self._early_cancels and next(iter(self._early_cancels.values())) <= now:
            self._early_cancels.popitem(last=False)
        self._early_cancels.pop(op_id, None)
        self._early_cancels[op_id] = now + contract.LIMITS["early_cancel_ttl_ms"] / 1000
        while len(self._early_cancels) > contract.LIMITS["early_cancels"]:
            self._early_cancels.popitem(last=False)

    def _cancelled_early(self, op_id: str) -> bool:
        expiry = self._early_cancels.pop(op_id, None)
        return expiry is not None and expiry > time.monotonic()

    def ensure_queue(self, runtime: Runtime) -> OfflineQueue:
        """The executor's own engine and transport, independent of every UI handle; created on first start."""
        with runtime.lock:
            if runtime.queue is not None:
                return runtime.queue
            store = runtime.require_store()
            if self._sources_factory is not None:
                sources = self._sources_factory(runtime)
            else:
                config = runtime.config
                engine = Engine(
                    EngineConfig(
                        cache_dir=config.cache_dir,
                        xhr_backend=config.xhr_backend,
                        antibot_delay_sec=config.antibot_delay_sec,
                        access=runtime.executor_access,
                    ),
                    transport=LazyTransport(self._transport_factory),
                    history=runtime.state.history,
                    events=runtime.sink,
                )
                runtime.executor_engine = engine
                sources = EngineSources(engine, runtime.executor_access)
            runtime.queue = OfflineQueue(
                runtime.state, store, sources, network=runtime.network, events=runtime.sink, **self._executor_options
            )
            return runtime.queue

    def _stop_push(self, runtime: Runtime, push: PushAdapter, timeout: float | None) -> bool:
        """An adapter is forgotten only once its thread has ended, so a later close still sees a stuck listener."""
        ended = push.stop(timeout)
        if ended:
            with self.lock:
                if push in runtime.ending_pushes:
                    runtime.ending_pushes.remove(push)
        return ended

    def _running_on(self, runtime: Runtime, exclude: str | None) -> int:
        return sum(1 for op in self._operations.values() if op.runtime is runtime and op.key != exclude)

    def close_runtime(self, runtime: Runtime, timeout: float | None, exclude: str | None = None) -> dict[str, Any]:
        """Handles and their operations, then the executor, then event threads, then platform objects; repeatable."""
        deadline = None if timeout is None else time.monotonic() + timeout

        def left() -> float | None:
            return None if deadline is None else max(deadline - time.monotonic(), 0.0)

        with self.lock:
            runtime.closing = True
            entries = [e for e in self._engines.values() if e.runtime is runtime]
            for entry in entries:
                self._detach(entry, exclude)
        for entry in entries:
            entry.engine.close()
        # a cancelled export or delete may still be using the file store; the runtime is kept until it returns
        with self.lock:
            self._operation_ended.wait_for(lambda: self._running_on(runtime, exclude) == 0, left())
            running = self._running_on(runtime, exclude)
            pushes = list(runtime.ending_pushes)
        alive = sum(1 for push in pushes if not self._stop_push(runtime, push, left()))
        executor_ended = runtime.queue.close(left()) if runtime.queue is not None else True
        watcher_ended = runtime.watcher.stop(left()) if executor_ended else False
        ended = executor_ended and watcher_ended and alive == 0 and running == 0
        if ended:
            if runtime.executor_engine is not None:
                runtime.executor_engine.close()
            runtime.release()
            with self.lock:
                if self._runtimes.get(runtime.config.db_path) is runtime:
                    del self._runtimes[runtime.config.db_path]
        return {
            "ended": ended,
            "executor_ended": executor_ended,
            "delivery_threads_alive": alive,
            "operations_running": running,
        }

    def _runtime_close(self, c: Call) -> Any:
        timeout = c.r.integer("timeout_ms", 10000, minimum=0, maximum=600000) / 1000
        if c.r.has("db_path"):
            path = Path(c.r.text("db_path")).resolve()
            with self.lock:
                runtime = self._runtimes.get(path)
            if runtime is None:
                return {"ended": True, "executor_ended": True, "delivery_threads_alive": 0, "operations_running": 0}
        else:
            runtime = c.entry.runtime
        return self.close_runtime(runtime, timeout, exclude=c.operation.key)

    def shutdown(self, timeout: float | None = None) -> str:
        """Closes every runtime; a false ``ended`` means some I/O still runs and the call may be repeated."""
        try:
            with self.lock:
                runtimes = list(self._runtimes.values())
            results = {str(rt.config.db_path): self.close_runtime(rt, timeout) for rt in runtimes}
            return _ok({"ended": all(r["ended"] for r in results.values()), "runtimes": results})
        except (Exception, EngineInterrupt) as exc:
            return _fail(exc)

    def _bridge_info(self, _c: Call) -> Any:
        return {
            "contract": contract.CONTRACT,
            "methods": sorted([*self._methods, "create_engine", "close_engine"]),
            "capabilities": {
                "interaction_grants": False,
                "events_pull": True,
                "events_push": True,
                "platform_storage": True,
                "platform_export": True,
                "cancel_before_start": True,
            },
            "limits": dict(contract.LIMITS),
        }

    def call(self, method_name: str, payload_json: str) -> str:
        """A read whose episode session closed meanwhile reports ``session_closed``; a committed mutation its outcome."""
        operation: Operation | None = None
        try:
            spec = self._methods.get(method_name)
            if spec is None:
                raise BridgeError("unknown_method", "no such method", method=method_name)
            r = Reader(parse(payload_json))
            r.only(spec.fields | contract.RESERVED_FIELDS)
            _check_contract(r)
            client_id = r.text("op_id", None)
            engine_id = r.text("engine_id") if spec.needs_engine or r.has("engine_id") else None
            with self.lock:
                entry = self.engine_entry(engine_id) if engine_id is not None else None
                if client_id is not None and client_id in self._client_ops:
                    raise BridgeError("operation_id_in_use", "an operation with this id is still running")
                if client_id is not None and self._cancelled_early(client_id):
                    raise OperationCancelled()
                operation = Operation(
                    key=uuid.uuid4().hex,
                    engine_id=engine_id,
                    token=CancelToken(),
                    client_id=client_id,
                    runtime=entry.runtime if entry is not None else None,
                )
                self._operations[operation.key] = operation
                if entry is not None:
                    entry.operations.add(operation.key)
                if client_id is not None:
                    self._client_ops[client_id] = operation.key
            token = CURRENT_OP.set(client_id)
            try:
                result = spec.handler(Call(self, r, operation, entry))
            finally:
                CURRENT_OP.reset(token)
            # one critical section with unregistering, so a close sees the operation running or not at all
            with self.lock:
                closed = _session_closed(operation)
                self._unregister(operation)
            return _fail(closed) if closed is not None else _ok(result)
        except (Exception, EngineInterrupt) as exc:
            if operation is not None:
                with self.lock:
                    closed = _session_closed(operation)
                    self._unregister(operation)
                return _fail(closed or exc)
            return _fail(exc)

    def _unregister(self, operation: Operation) -> None:
        with self.lock:
            if self._operations.pop(operation.key, None) is None:
                return
            if operation.client_id is not None and self._client_ops.get(operation.client_id) == operation.key:
                del self._client_ops[operation.client_id]
            if operation.engine_id is not None and (entry := self._engines.get(operation.engine_id)) is not None:
                entry.operations.discard(operation.key)
            if operation.session is not None:
                operation.session.operations.discard(operation.key)
            for handle in operation.platform_handles:
                self.platform.release_use(handle)
            self._operation_ended.notify_all()


def _backend(r: Reader) -> XhrBackendName:
    text = r.text("xhr_backend", "new")
    try:
        parse_backend(text)
    except ValueError:
        raise InputError("xhr_backend", "unknown_value") from None
    return cast(XhrBackendName, text)


def _session_closed(operation: Operation) -> BridgeError | None:
    session = operation.session
    if operation.committed or session is None or session.closed_by is None:
        return None
    return BridgeError(
        "session_closed",
        "the episode session was closed while the operation was running",
        session_id=session.session_id,
        closed_by=session.closed_by,
    )


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data})


def _fail(exc: BaseException) -> str:
    return json.dumps({"ok": False, "error": describe(exc)})
