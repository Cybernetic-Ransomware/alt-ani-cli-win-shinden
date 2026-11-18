"""The engine: one HTTP session per provider and access policy, handed out to operations as generation-checked leases."""

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Self

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.config import AccessConfig, EngineConfig
from alt_ani_core.errors import EngineClosedError, ParseError, ShindenError, StaleSessionError
from alt_ani_core.events import NULL_SINK, EventSink
from alt_ani_core.history.store import HistoryStore
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.models import EpisodeListing, EpisodeRow, PlayerEntry, SearchResults, SeriesMetadata, SeriesRef
from alt_ani_core.providers.api import EpisodeEmbeds, Provider
from alt_ani_core.providers.shinden import ShindenProvider
from alt_ani_core.rules.health import ResolverHealth
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.transport.api import HttpResponse, HttpSession, SessionProfile, Transport, TransportError

MAX_METADATA_WORKERS = 8


@dataclass
class _Slot:
    """One provider's session of one generation; closed only once retired and no operation still leases it."""

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
    """What one engine call works with: the provider's leased session, a guarded transport and the liveness check."""

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
        providers: Sequence[Provider] | None = None,
    ) -> None:
        """``providers`` defaults to Shinden alone; the first one serves ``search``."""
        self.config = config
        self.history = history
        self.events = events
        self.clock = clock
        self.health = ResolverHealth(clock=clock)
        self._transport = transport
        if providers is None:
            providers = (
                ShindenProvider(xhr_backend=config.xhr_backend, antibot_delay_sec=config.antibot_delay_sec, events=events),
            )
        if not providers or len({p.name for p in providers}) != len(providers):
            raise ValueError("an engine needs at least one provider, each with its own name")
        self._providers = tuple(providers)
        self._by_name = {p.name: p for p in self._providers}
        self._state_lock = threading.RLock()
        self._access = config.access
        self._generation = 0
        # every provider's session belongs to the one current generation; retiring it retires them all
        self._slots: dict[str, _Slot] = {}
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
        slots, self._slots = self._slots, {}
        for slot in slots.values():
            slot.retired = True
            if slot.leases == 0:
                slot.session.close()

    def _acquire(self, provider: Provider) -> _Slot:
        while True:
            with self._state_lock:
                if self._closed:
                    raise EngineClosedError("engine is closed")
                slot = self._slots.get(provider.name)
                if slot is not None:
                    slot.leases += 1
                    return slot
                generation, access = self._generation, self._access
            # Shinden's clearance round-trip can take over a minute, so the profile is built outside the lock
            profile = provider.session_profile(access, self.config.cache_dir)
            session = self._transport.open(profile)
            with self._state_lock:
                if not self._closed and generation == self._generation and provider.name not in self._slots:
                    self._slots[provider.name] = _Slot(generation=generation, session=session, profile=profile)
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

    def _route(self, url: str, *, kind: str) -> Provider:
        """The provider owning the URL; one no provider owns is refused before any request."""
        found = next((p for p in self._providers if p.owns(url)), None)
        if found is None:
            raise ParseError(EXCEPTIONS[kind]["parse_error"].format(url=repr(url)))
        return found

    @contextmanager
    def operation(
        self, cancel: CancelToken = NEVER, *, generation: int | None = None, provider: str | None = None
    ) -> Iterator[Operation]:
        """Leases one provider's session (default: the first) for one call; ``generation`` pins it to an EpisodeSession's."""
        chosen = self._providers[0] if provider is None else self._by_name[provider]
        cancel.raise_if_cancelled()
        if generation is not None:
            # checked before leasing, so a stale episode session never opens a new HTTP session just to be refused
            with self._state_lock:
                if self._closed:
                    raise EngineClosedError("engine is closed")
                if generation != self._generation:
                    raise StaleSessionError()
        slot = self._acquire(chosen)
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

    def search(self, query: str, *, cancel: CancelToken = NEVER) -> SearchResults:
        provider = self._providers[0]
        with self.operation(cancel, provider=provider.name) as op:
            return provider.search(op.client, query, cancel=cancel)

    def series_from_url(self, url: str) -> SeriesRef:
        url = url.strip()
        return self._route(url, kind="series").series_from_url(url)

    def metadata(self, ref: SeriesRef, *, cancel: CancelToken = NEVER) -> SeriesMetadata:
        provider = self._route(ref.url, kind="series")
        with self.operation(cancel, provider=provider.name) as op:
            return provider.metadata(op.client, ref)

    def metadata_many(self, refs: Sequence[SeriesRef], *, cancel: CancelToken = NEVER) -> list[SeriesMetadata | None]:
        """Concurrent per provider, with one HTTP session per worker thread; None marks a series that could not be read."""
        results: list[SeriesMetadata | None] = [None] * len(refs)
        routed = [next((p for p in self._providers if p.owns(ref.url)), None) for ref in refs]
        for provider in self._providers:
            indexes = [i for i, owner in enumerate(routed) if owner is provider]
            if indexes:
                found = self._metadata_batch(provider, [refs[i] for i in indexes], cancel)
                for i, meta in zip(indexes, found, strict=True):
                    results[i] = meta
        return results

    def _metadata_batch(
        self, provider: Provider, refs: Sequence[SeriesRef], cancel: CancelToken
    ) -> list[SeriesMetadata | None]:
        with self.operation(cancel, provider=provider.name) as op:
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
                    return provider.metadata(client, ref)
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
        provider = self._route(ref.url, kind="series")
        with self.operation(cancel, provider=provider.name) as op:
            return provider.episodes(op.client, ref, cancel=cancel)

    def open_episode(self, episode: EpisodeRow, *, cancel: CancelToken = NEVER) -> EpisodeSession:
        """Always a new session with fresh limits, also for an episode that was opened before."""
        provider = self._route(episode.url, kind="episode")
        with self.operation(cancel, provider=provider.name) as op:
            players, source = self.load_episode(op.client, episode)
            return EpisodeSession(self, op.generation, episode, players, source, provider=provider.name)

    def load_episode(self, client: HttpSession, episode: EpisodeRow) -> tuple[list[PlayerEntry], EpisodeEmbeds]:
        """``client`` must be a session of the provider owning the episode, as leased by ``operation``."""
        return self._route(episode.url, kind="episode").load_episode(client, episode)
