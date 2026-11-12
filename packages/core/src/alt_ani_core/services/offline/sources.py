"""Where an attempt gets its sources from: a fresh episode session per attempt, nothing of it is persisted."""

import dataclasses
import threading
from collections.abc import Sequence
from typing import Protocol

from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import AccessBlockedError, ShindenError, ShindenXhrError, SourcesExhaustedError
from alt_ani_core.models import EpisodeRow, FallbackPolicy, PlayerEntry, Stream
from alt_ani_core.rules import ranking
from alt_ani_core.rules.conditions import flaresolverr_allowed
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.services.offline.interaction import SCOPE_HOST, SCOPE_SHINDEN, InteractionGrant, InteractionNeeded
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.state import NetworkKind

_INTERACTION_ERRORS = {"JavaScriptRequiredError"}


class SourceUnavailable(Exception):  # noqa: N818 - a condition the attempt handles, not a crash
    """This source produced no stream now; ``transient`` failures may work later, others will not."""

    def __init__(self, reason: str, *, transient: bool = True, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.transient = transient
        self.retry_after = retry_after


class EpisodeSources(Protocol):
    @property
    def entries(self) -> Sequence[PlayerEntry]: ...

    def resolve(self, entry: PlayerEntry, *, fresh: bool, cancel: CancelToken) -> Stream: ...


class SourceProvider(Protocol):
    def open(self, episode: EpisodeRow, *, grant: InteractionGrant | None, cancel: CancelToken) -> EpisodeSources: ...

    def network_changed(self, network: NetworkKind) -> None: ...


class _SessionSources:
    def __init__(self, session: EpisodeSession) -> None:
        self._session = session
        ranked = ranking.sort_players(list(session.sources), download=True)
        self._entries = tuple(session.rank(ranked))

    @property
    def entries(self) -> Sequence[PlayerEntry]:
        return self._entries

    def resolve(self, entry: PlayerEntry, *, fresh: bool, cancel: CancelToken) -> Stream:
        try:
            resolved = self._session.resolve(entry, policy=FallbackPolicy.SINGLE, force_fresh=fresh, cancel=cancel)
        except SourcesExhaustedError as exc:
            if any(attempt.error in _INTERACTION_ERRORS for attempt in exc.attempts):
                host = next((a.host for a in exc.attempts if a.host), None)
                raise InteractionNeeded(SCOPE_HOST, host=host, online_id=entry.online_id) from None
            raise SourceUnavailable("exhausted") from None
        except ShindenXhrError as exc:
            outcome = shinden_api.xhr_outcome(exc)
            if outcome is shinden_api.XhrOutcome.ACCESS_BLOCKED:
                raise InteractionNeeded(SCOPE_SHINDEN) from None
            # the shared API stopping (rate limit, outage) is not this source's fault: try again later
            transient = outcome is not shinden_api.XhrOutcome.NEXT_PLAYER
            raise SourceUnavailable("shinden_api", transient=transient, retry_after=exc.retry_after) from None
        except AccessBlockedError:
            raise InteractionNeeded(SCOPE_SHINDEN) from None
        except ShindenError as exc:
            raise SourceUnavailable(exc.code or "source_error") from None
        return resolved.stream


class EngineSources:
    """Backed by the executor's own Engine, whose FlareSolverr use follows the network: never on mobile data."""

    def __init__(self, engine: Engine, base_access: AccessConfig) -> None:
        self._engine = engine
        self._base = base_access
        self._lock = threading.Lock()
        self._applied: bool | None = None

    def network_changed(self, network: NetworkKind) -> None:
        use = flaresolverr_allowed(self._base.use_flaresolverr, network)
        with self._lock:
            if use == self._applied:
                return
            self._applied = use
        self._engine.set_access(dataclasses.replace(self._base, use_flaresolverr=use))

    def open(self, episode: EpisodeRow, *, grant: InteractionGrant | None, cancel: CancelToken) -> EpisodeSources:
        # a grant would be injected into the engine's session here; that needs the stage-5 interaction flow
        try:
            return _SessionSources(self._engine.open_episode(episode, cancel=cancel))
        except AccessBlockedError:
            raise InteractionNeeded(SCOPE_SHINDEN) from None
        except ShindenError as exc:
            raise SourceUnavailable(exc.code or "episode_unavailable") from None
