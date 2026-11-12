"""One opened episode: its player list, XHR state, embed cache and limits; operations on it are serialized."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alt_ani_core import diagnostics, extract
from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.config import ExtractOptions
from alt_ani_core.errors import (
    AntiBotError,
    EngineInterrupt,
    FreshResolveLimitError,
    NoStreamError,
    ReloadLimitError,
    ShindenError,
    ShindenXhrError,
    SourcesExhaustedError,
)
from alt_ani_core.events import (
    EmbedResolved,
    ExtractorFallback,
    HostDeferred,
    HostDropped,
    HostLastResort,
    PhaseFinished,
    PhaseStarted,
    SourceAttemptFailed,
)
from alt_ani_core.models import (
    EmbedURL,
    EpisodeRow,
    FallbackPolicy,
    PlayerEntry,
    PlayerSource,
    Purpose,
    ResolvedStream,
    SourceAttempt,
    SourceFilter,
    SourceSelection,
    Stream,
)
from alt_ani_core.redaction import redact_text
from alt_ani_core.rules import ranking
from alt_ani_core.rules.filtering import filter_players
from alt_ani_core.rules.health import HealthTransition, Signal, classify_failure
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.shinden.xhr import EpisodeXhr

if TYPE_CHECKING:
    from alt_ani_core.services.engine import Engine, Operation

PHASE_ANTIBOT = "antibot_delay"
MAX_FRESH_RESOLVES = 2

_XHR_FAILURE_FIELDS = ("stage", "api_code", "retry_after", "reason")


@dataclass
class _Deferred:
    """The embed is kept so the last-resort pass retries extraction without a second player_show."""

    candidate: PlayerEntry
    embed: EmbedURL
    host: str


class EpisodeSession:
    def __init__(
        self, engine: Engine, generation: int, episode: EpisodeRow, players: Sequence[PlayerEntry], xhr: EpisodeXhr
    ) -> None:
        self.episode = episode
        self._engine = engine
        self._generation = generation
        self._lock = threading.RLock()
        self._players = tuple(players)
        self._xhr = xhr
        self._embeds: dict[str, EmbedURL] = {}
        self._fresh_resolves: dict[str, int] = {}
        self._reloaded = False

    @property
    def sources(self) -> tuple[PlayerEntry, ...]:
        with self._lock:
            return self._players

    @property
    def reload_used(self) -> bool:
        with self._lock:
            return self._reloaded

    @property
    def known_sources(self) -> dict[str, PlayerSource]:
        """Hosts learned so far, by online_id; a source is known once its embed resolved, whether or not it then played."""
        with self._lock:
            return {
                online_id: PlayerSource(online_id=online_id, host=_display_host(embed.url), embed_url=embed.url)
                for online_id, embed in self._embeds.items()
            }

    def select(self, flt: SourceFilter, *, purpose: Purpose) -> SourceSelection:
        """Never widens the pool itself: with ``unmatched`` set, using ``all_sources`` is the caller's decision."""
        ordered = ranking.sort_players(list(self.sources), download=purpose is Purpose.DOWNLOAD)
        filtered, unmatched = filter_players(ordered, flt)
        return SourceSelection(candidates=tuple(filtered), unmatched=tuple(unmatched), all_sources=tuple(ordered))

    def rank(self, players: Sequence[PlayerEntry]) -> list[PlayerEntry]:
        """Health-aware order for unattended runs; a person's own pick is never reordered."""
        health = self._engine.health
        before = list(players)
        ordered = health.order(before)
        if [p.online_id for p in ordered] != [p.online_id for p in before]:
            diagnostics.health_reorder(
                number=self.episode.number,
                before=",".join(p.online_id for p in before),
                after=",".join(p.online_id for p in ordered),
                states=",".join(health.predict(p.player).value for p in ordered),
            )
        return ordered

    def reload(self, *, cancel: CancelToken = NEVER) -> None:
        """Re-reads the episode page in place, once; the embed cache goes with the old player list."""
        with self._lock:
            if self._reloaded:
                raise ReloadLimitError("the player list was already reloaded once").coded(
                    "episode.reload_limit", number=self.episode.number
                )
            self._reloaded = True
            try:
                with self._engine.operation(cancel, generation=self._generation) as op:
                    players, xhr = self._engine.load_episode(op.client, self.episode)
                    op.check()
                    self._players, self._xhr, self._embeds = tuple(players), xhr, {}
            except EngineInterrupt:
                # an interrupted reload reloaded nothing, so it does not use up the one allowed reload
                self._reloaded = False
                raise

    def probe_hosts(self, candidates: Sequence[PlayerEntry], *, cancel: CancelToken = NEVER) -> ShindenXhrError | None:
        """Learns hosts one source at a time, so a block or outage stops before the next XHR; returns that stop error."""
        with self._lock, self._engine.operation(cancel, generation=self._generation) as op:
            pending = [p for p in candidates if p.online_id not in self._embeds]
            if not pending:
                return None
            self._xhr.begin_operation()
            for candidate in pending:
                try:
                    embed = self._resolve_embed(op, candidate.online_id, cancel)
                except ShindenXhrError as exc:
                    op.check()
                    if shinden_api.xhr_outcome(exc) is shinden_api.XhrOutcome.NEXT_PLAYER:
                        continue
                    return exc
                except ShindenError:
                    op.check()
                    continue
                self._embeds[candidate.online_id] = embed
            return None

    def resolve(
        self,
        chosen: PlayerEntry,
        *,
        candidates: Sequence[PlayerEntry] = (),
        policy: FallbackPolicy = FallbackPolicy.SINGLE,
        options: ExtractOptions | None = None,
        force_fresh: bool = False,
        cancel: CancelToken = NEVER,
    ) -> ResolvedStream:
        """One operation with one token-refresh budget.

        SINGLE tries only ``chosen``. WALK starts with ``chosen`` and walks ``candidates``; a host already known
        unavailable is deferred to the end and still gets one real attempt if nothing else worked.
        A cached embed skips the anti-bot wait; if extraction on it fails, one fresh resolve follows.
        ``force_fresh`` drops the chosen source's cached embed first (an expired stream URL), a bounded number of times.
        Raises SourcesExhaustedError when nothing worked, and a ShindenXhrError whose outcome is not NEXT_PLAYER
        as it is: that is the shared API failing, not a source. Cancellation and a stale session record nothing.
        """
        with self._lock, self._engine.operation(cancel, generation=self._generation) as op:
            if force_fresh:
                self._spend_fresh_resolve(chosen)
                try:
                    return self._resolve(op, chosen, candidates, policy, options, cancel)
                except EngineInterrupt:
                    # an interrupted attempt is not a spent one
                    self._fresh_resolves[chosen.online_id] -= 1
                    raise
            return self._resolve(op, chosen, candidates, policy, options, cancel)

    def _resolve(
        self,
        op: Operation,
        chosen: PlayerEntry,
        candidates: Sequence[PlayerEntry],
        policy: FallbackPolicy,
        options: ExtractOptions | None,
        cancel: CancelToken,
    ) -> ResolvedStream:
        self._xhr.begin_operation()
        walk = policy is FallbackPolicy.WALK
        order = [chosen, *(p for p in candidates if p is not chosen)] if walk else [chosen]
        attempts: list[SourceAttempt] = []
        deferred: list[_Deferred] = []

        for candidate in order:
            op.check()
            if walk:
                diagnostics.player_selected(candidate.online_id, candidate.player, None)
            resolved = self._attempt(op, candidate, options, cancel, attempts, deferred if walk else None)
            if resolved is not None:
                return resolved

        resolved = self._last_resort(op, deferred, options, cancel, attempts)
        if resolved is not None:
            return resolved
        raise SourcesExhaustedError(
            f"no source of episode {self.episode.number:g} produced a stream", attempts=tuple(attempts)
        ).coded("episode.sources_exhausted", number=self.episode.number)

    def _spend_fresh_resolve(self, chosen: PlayerEntry) -> None:
        used = self._fresh_resolves.get(chosen.online_id, 0)
        if used >= MAX_FRESH_RESOLVES:
            raise FreshResolveLimitError("this source was already re-resolved as often as allowed").coded(
                "episode.fresh_resolve_limit", limit=MAX_FRESH_RESOLVES
            )
        self._fresh_resolves[chosen.online_id] = used + 1
        self._embeds.pop(chosen.online_id, None)

    def _attempt(
        self,
        op: Operation,
        candidate: PlayerEntry,
        options: ExtractOptions | None,
        cancel: CancelToken,
        attempts: list[SourceAttempt],
        deferred: list[_Deferred] | None,
    ) -> ResolvedStream | None:
        health = self._engine.health
        start = self._engine.clock()
        host: str | None = None
        try:
            cached = self._embeds.get(candidate.online_id)
            embed = cached if cached is not None else self._fresh_embed(op, candidate, cancel)
            host = diagnostics.host_of_url(embed.url)
            health.learn_label(candidate.player, host)

            if deferred is not None and health.should_defer(host):
                deferred.append(_Deferred(candidate=candidate, embed=embed, host=host))
                self._defer_diag("deferred", candidate, host)
                self._engine.events.emit(HostDeferred(host=host))
                return None

            try:
                stream = self._extract(op, embed, options, cancel)
            except NoStreamError:
                if cached is None:
                    raise
                op.check()
                embed = self._fresh_embed(op, candidate, cancel)
                host = diagnostics.host_of_url(embed.url)
                health.learn_label(candidate.player, host)
                stream = self._extract(op, embed, options, cancel)
        except (NoStreamError, AntiBotError, ShindenXhrError) as exc:
            # an interrupted operation is not this source's failure: no penalty, no record, no next candidate
            op.check()
            self._record_failure(host, candidate, exc, start)
            attempts.append(SourceAttempt(source=candidate, host=host, error=type(exc).__name__))
            if isinstance(exc, ShindenXhrError) and shinden_api.xhr_outcome(exc) is not shinden_api.XhrOutcome.NEXT_PLAYER:
                raise
            return None
        op.check()
        self._record_success(host, candidate, start)
        return ResolvedStream(stream=stream, source=candidate, embed=embed)

    def _last_resort(
        self,
        op: Operation,
        deferred: list[_Deferred],
        options: ExtractOptions | None,
        cancel: CancelToken,
        attempts: list[SourceAttempt],
    ) -> ResolvedStream | None:
        health = self._engine.health
        retried_hosts: set[str] = set()
        for entry in deferred:
            op.check()
            if entry.host in retried_hosts and health.should_defer(entry.host):
                self._defer_diag("dropped", entry.candidate, entry.host)
                self._engine.events.emit(HostDropped(host=entry.host))
                continue

            diagnostics.player_selected(entry.candidate.online_id, entry.candidate.player, None)
            self._defer_diag("retry", entry.candidate, entry.host)
            self._engine.events.emit(HostLastResort(host=entry.host))
            retried_hosts.add(entry.host)

            start = self._engine.clock()
            try:
                stream = self._extract(op, entry.embed, options, cancel)
            except NoStreamError as exc:
                op.check()
                self._record_failure(entry.host, entry.candidate, exc, start)
                attempts.append(SourceAttempt(source=entry.candidate, host=entry.host, error=type(exc).__name__))
                continue
            op.check()
            self._record_success(entry.host, entry.candidate, start)
            return ResolvedStream(stream=stream, source=entry.candidate, embed=entry.embed)
        return None

    def _fresh_embed(self, op: Operation, candidate: PlayerEntry, cancel: CancelToken) -> EmbedURL:
        events = self._engine.events
        events.emit(PhaseStarted(phase=PHASE_ANTIBOT))
        try:
            embed = self._resolve_embed(op, candidate.online_id, cancel)
        finally:
            events.emit(PhaseFinished(phase=PHASE_ANTIBOT))
        self._embeds[candidate.online_id] = embed
        return embed

    def _resolve_embed(self, op: Operation, online_id: str, cancel: CancelToken) -> EmbedURL:
        embed = shinden_api.resolve_embed(
            op.client,
            online_id,
            xhr=self._xhr,
            min_sleep_seconds=self._engine.config.antibot_delay_sec,
            cancel=cancel,
        )
        op.check()
        return embed

    def _extract(self, op: Operation, embed: EmbedURL, options: ExtractOptions | None, cancel: CancelToken) -> Stream:
        events = self._engine.events
        # the only place the embed URL is reported; failure messages carry the host only
        events.emit(EmbedResolved(redacted_url=redact_text(embed.url)))
        return extract.resolve(
            embed.url,
            embed.referer,
            transport=op.transport,
            options=options,
            on_fallback=lambda kind, host, text: events.emit(ExtractorFallback(kind=kind, host=host, redacted_text=text)),
            cancel=cancel,
            check=op.check,
        )

    def _defer_diag(self, action: str, candidate: PlayerEntry, host: str) -> None:
        diagnostics.health_defer(
            action=action,
            host=host,
            online_id=candidate.online_id,
            player=candidate.player,
            state=self._engine.health.state(host).value,
            category=None,
            http_status=None,
        )

    def _xhr_diag(self) -> dict:
        xhr = self._xhr
        return {"xhr_backend": xhr.backend, "token_refreshed": xhr.refresh_used, "xhr_fallback": xhr.fallback_reason}

    def _record_success(self, host: str | None, candidate: PlayerEntry, start: float) -> None:
        if host is not None:
            _emit_transition(
                self._engine.health.record(host, candidate.online_id, Signal.SUCCESS), category=None, http_status=None
            )
        diagnostics.resolve_result(host, True, None, self._engine.clock() - start, **self._xhr_diag())

    def _record_failure(self, host: str | None, candidate: PlayerEntry, exc: ShindenError, start: float) -> None:
        fields = _failure_fields(exc)
        if host is not None:
            signal = classify_failure(
                host,
                layer=fields.get("layer") or "",
                category=fields.get("category"),
                http_status=fields.get("http_status"),
                used_fallback=bool(fields.get("used_fallback")),
                fallback_category=fields.get("fallback_category"),
                fallback_http_status=fields.get("fallback_http_status"),
            )
            transition = self._engine.health.record(host, candidate.online_id, signal)
            _emit_transition(transition, category=fields.get("category"), http_status=fields.get("http_status"))
        diagnostics.resolve_result(host, False, type(exc).__name__, self._engine.clock() - start, **fields, **self._xhr_diag())
        # a shared-API stop is not this source's failure; the caller reports it from the raised error
        if fields.get("xhr_outcome") in (None, shinden_api.XhrOutcome.NEXT_PLAYER):
            self._engine.events.emit(
                SourceAttemptFailed(player=candidate.player, episode=self.episode.number, redacted_text=redact_text(exc))
            )


def _display_host(url: str) -> str:
    return diagnostics.host_of_url(url).removeprefix("www.")


def _failure_fields(exc: ShindenError) -> dict:
    fields: dict = dict.fromkeys(
        (
            "layer",
            "category",
            "http_status",
            "used_fallback",
            "fallback_layer",
            "fallback_category",
            "fallback_http_status",
            *_XHR_FAILURE_FIELDS,
        )
    )
    failure = exc.failure if isinstance(exc, NoStreamError) else None
    if failure is not None:
        fields.update(
            layer=failure.layer,
            category=failure.category,
            http_status=failure.http_status,
            used_fallback=failure.used_fallback,
            fallback_layer=failure.fallback_layer,
            fallback_category=failure.fallback_category,
            fallback_http_status=failure.fallback_http_status,
        )
    if isinstance(exc, ShindenXhrError):
        fields.update(layer=exc.layer, category=exc.category, http_status=exc.http_status)
        fields.update({name: getattr(exc, name) for name in _XHR_FAILURE_FIELDS})
        fields["xhr_outcome"] = shinden_api.xhr_outcome(exc).value
    elif isinstance(exc, AntiBotError):
        fields.update(layer="shinden_api", category="anti_bot")
    return fields


def _emit_transition(transition: HealthTransition | None, *, category: str | None, http_status: int | None) -> None:
    if transition is None:
        return
    diagnostics.host_health(
        host=transition.host,
        from_state=transition.before.value,
        to_state=transition.after.value,
        signal=transition.signal.value,
        online_id=transition.online_id,
        category=category,
        http_status=http_status,
        resolver=extract.resolver_family(transition.host),
        evidence_expired=transition.evidence_expired,
    )
