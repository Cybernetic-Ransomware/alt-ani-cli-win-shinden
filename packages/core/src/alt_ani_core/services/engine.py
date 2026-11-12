"""The engine: one Shinden HTTP session per access policy, handed out to operations as generation-checked leases."""

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Self

from alt_ani_core import diagnostics
from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.config import AccessConfig, EngineConfig
from alt_ani_core.errors import EngineClosedError, ShindenError, StaleSessionError
from alt_ani_core.events import NULL_SINK, EventSink, XhrFallback
from alt_ani_core.history.store import HistoryStore
from alt_ani_core.models import EpisodeListing, EpisodeRow, PlayerEntry, SeriesHit, SeriesMetadata, SeriesRef
from alt_ani_core.rules.health import ResolverHealth
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.shinden import metadata as shinden_metadata
from alt_ani_core.shinden import search as shinden_search
from alt_ani_core.shinden import series as shinden_series
from alt_ani_core.shinden import xhr as shinden_xhr
from alt_ani_core.shinden.xhr import EpisodeXhr
from alt_ani_core.transport import clearance
from alt_ani_core.transport.api import HttpResponse, HttpSession, SessionProfile, Transport, TransportError

MAX_METADATA_WORKERS = 8


@dataclass
class _Slot:
    """The Shinden session of one generation; closed only once retired and no operation still leases it."""

    generation: int
    session: HttpSession
    profile: SessionProfile
    leases: int = 0
    retired: bool = False


class GuardedSession:
    """Checks cancellation and engine liveness around every request, so an in-flight result is discarded, not used."""

    def __init__(self, inner: HttpSession, check: Callable[[], None], *, owns_inner: bool = False) -> None:
        self._inner = inner
        self._check = check
        self._owns_inner = owns_inner

    def get(
        self, url: str, *, headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None
    ) -> HttpResponse:
        return self._send(lambda: self._inner.get(url, headers=headers, params=params))

    def post(self, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None, data: Any = None) -> HttpResponse:
        return self._send(lambda: self._inner.post(url, headers=headers, json=json, data=data))

    def _send(self, request: Callable[[], HttpResponse]) -> HttpResponse:
        self._check()
        try:
            response = request()
        except Exception as exc:
            failure = exc
        else:
            self._check()
            return response
        # checked outside the except block, so the interrupt carries no transport error (and its URL) as context
        self._check()
        raise failure

    def close(self) -> None:
        if self._owns_inner:
            self._inner.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class GuardedTransport:
    """Extractor sessions stay isolated per call; each one gets the operation's checks."""

    def __init__(self, inner: Transport, check: Callable[[], None]) -> None:
        self._inner = inner
        self._check = check

    def open(self, profile: SessionProfile) -> HttpSession:
        self._check()
        return GuardedSession(self._inner.open(profile), self._check, owns_inner=True)


@dataclass(frozen=True)
class Operation:
    """What one engine call works with: the leased Shinden session, a guarded transport and the liveness check."""

    client: HttpSession
    transport: Transport
    check: Callable[[], None]
    generation: int
    profile: SessionProfile


class Engine:
    def __init__(
        self,
        config: EngineConfig,
        *,
        transport: Transport,
        history: HistoryStore,
        events: EventSink = NULL_SINK,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.history = history
        self.events = events
        self.clock = clock
        self.health = ResolverHealth(clock=clock)
        self._transport = transport
        self._xhr_backend = shinden_xhr.parse_backend(config.xhr_backend)
        self._state_lock = threading.RLock()
        self._access = config.access
        self._generation = 0
        self._slot: _Slot | None = None
        self._closed = False

    @property
    def generation(self) -> int:
        with self._state_lock:
            return self._generation

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def set_access(self, access: AccessConfig) -> None:
        """New policy means a new session and cookie jar; everything opened before is stale from here on."""
        with self._state_lock:
            if self._closed:
                raise EngineClosedError("engine is closed")
            self._access = access
            self._retire_locked()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._retire_locked()

    def _retire_locked(self) -> None:
        self._generation += 1
        slot, self._slot = self._slot, None
        if slot is not None:
            slot.retired = True
            if slot.leases == 0:
                slot.session.close()

    def _acquire(self) -> _Slot:
        while True:
            with self._state_lock:
                if self._closed:
                    raise EngineClosedError("engine is closed")
                if self._slot is not None:
                    self._slot.leases += 1
                    return self._slot
                generation, access = self._generation, self._access
            # the clearance round-trip can take over a minute, so it runs outside the lock
            profile = clearance.shinden_profile(clearance.get_clearance(access, self.config.cache_dir, self.events))
            session = self._transport.open(profile)
            with self._state_lock:
                if not self._closed and generation == self._generation and self._slot is None:
                    self._slot = _Slot(generation=generation, session=session, profile=profile)
                    continue
            session.close()

    def _release(self, slot: _Slot) -> None:
        with self._state_lock:
            slot.leases -= 1
            if slot.retired and slot.leases == 0:
                slot.session.close()

    def check_alive(self, generation: int) -> None:
        with self._state_lock:
            if self._closed or generation != self._generation:
                raise StaleSessionError()

    @contextmanager
    def operation(self, cancel: CancelToken = NEVER, *, generation: int | None = None) -> Iterator[Operation]:
        """Leases the session for one call; ``generation`` pins the call to the session an EpisodeSession was opened on."""
        cancel.raise_if_cancelled()
        if generation is not None:
            # checked before leasing, so a stale episode session never opens a new HTTP session just to be refused
            with self._state_lock:
                if self._closed:
                    raise EngineClosedError("engine is closed")
                if generation != self._generation:
                    raise StaleSessionError()
        slot = self._acquire()
        try:
            if generation is not None and slot.generation != generation:
                raise StaleSessionError()

            def check() -> None:
                cancel.raise_if_cancelled()
                self.check_alive(slot.generation)

            yield Operation(
                client=GuardedSession(slot.session, check),
                transport=GuardedTransport(self._transport, check),
                check=check,
                generation=slot.generation,
                profile=slot.profile,
            )
            # a result computed on a session that went stale meanwhile is never returned
            check()
        finally:
            self._release(slot)

    def search(self, query: str, *, cancel: CancelToken = NEVER) -> list[SeriesHit]:
        with self.operation(cancel) as op:
            return shinden_search.search_series(op.client, query)

    def series_from_url(self, url: str) -> SeriesRef:
        return shinden_series.parse_series_url(url)

    def metadata(self, ref: SeriesRef, *, cancel: CancelToken = NEVER) -> SeriesMetadata:
        with self.operation(cancel) as op:
            return shinden_metadata.fetch_series_metadata(op.client, ref)

    def metadata_many(self, refs: Sequence[SeriesRef], *, cancel: CancelToken = NEVER) -> list[SeriesMetadata | None]:
        """Concurrent, with one HTTP session per worker thread; None marks a series whose page could not be read."""
        if not refs:
            return []
        with self.operation(cancel) as op:
            local = threading.local()
            opened: list[HttpSession] = []
            opened_lock = threading.Lock()

            def fetch(ref: SeriesRef) -> SeriesMetadata | None:
                op.check()
                client = getattr(local, "client", None)
                if client is None:
                    client = local.client = op.transport.open(op.profile)
                    with opened_lock:
                        opened.append(client)
                try:
                    return shinden_metadata.fetch_series_metadata(client, ref)
                except TransportError, ShindenError:
                    return None

            try:
                with ThreadPoolExecutor(max_workers=min(MAX_METADATA_WORKERS, len(refs))) as pool:
                    futures = [pool.submit(fetch, ref) for ref in refs]
                return [future.result() for future in futures]
            finally:
                for client in opened:
                    client.close()

    def episodes(self, ref: SeriesRef, *, cancel: CancelToken = NEVER) -> EpisodeListing:
        with self.operation(cancel) as op:
            return shinden_series.list_episodes(op.client, ref, cancel=cancel)

    def open_episode(self, episode: EpisodeRow, *, cancel: CancelToken = NEVER) -> EpisodeSession:
        """Always a new session with fresh limits, also for an episode that was opened before."""
        with self.operation(cancel) as op:
            players, xhr = self.load_episode(op.client, episode)
            return EpisodeSession(self, op.generation, episode, players, xhr)

    def load_episode(self, client: HttpSession, episode: EpisodeRow) -> tuple[list[PlayerEntry], EpisodeXhr]:
        players, xhr = shinden_xhr.load_episode(client, episode.url, backend=self._xhr_backend)
        if xhr.forced:
            diagnostics.shinden_xhr_context(action="forced", backend=xhr.backend, reason=None)
        elif xhr.fallback_reason is not None and players:
            diagnostics.shinden_xhr_context(action="fallback", backend=xhr.backend, reason=xhr.fallback_reason)
            self.events.emit(XhrFallback(episode=episode.number, reason=xhr.fallback_reason))
        return players, xhr
