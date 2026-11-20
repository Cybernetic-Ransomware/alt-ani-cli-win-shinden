"""Preparing playback of one episode: a matching local copy first, otherwise a stream chosen by explicit criteria."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.errors import (
    CriteriaMismatchError,
    InteractionRequiredError,
    PlaybackBlockedError,
    SourcesExhaustedError,
    UnknownRecordError,
)
from alt_ani_core.history.local import LocalState
from alt_ani_core.models import FallbackPolicy, PlayerEntry, Stream, StreamVariant
from alt_ani_core.rules.criteria import (
    NoMatch,
    item_matches,
    match_source,
    match_variant,
    no_match_reason,
    variant_identity,
)
from alt_ani_core.rules.network import CellularConsent, stream_allowed
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.state import DownloadCriteria, EpisodeKey, ItemStatus, NetworkKind, OfflineItem, VariantIdentity
from alt_ani_core.storage.api import FileStore, PlaybackTarget

_INTERACTION_ERRORS = {"JavaScriptRequiredError"}


@dataclass(frozen=True)
class PlaybackSelection:
    """What a refresh must keep: the source, the exact variant and the criteria; never a URL."""

    online_id: str
    variant: VariantIdentity
    criteria: DownloadCriteria


@dataclass(frozen=True)
class LocalPlayback:
    item: OfflineItem
    target: PlaybackTarget
    other_copies: tuple[OfflineItem, ...] = ()


@dataclass(frozen=True)
class OnlinePlayback:
    source: PlayerEntry
    stream: Stream
    variant: StreamVariant
    selection: PlaybackSelection
    matches_criteria: bool
    variant_changed: bool = False
    other_copies: tuple[OfflineItem, ...] = ()


def no_match(reason: str) -> CriteriaMismatchError:
    return CriteriaMismatchError(f"nothing matches the {reason} criterion").coded("criteria.no_match", reason=reason)


def check_stream(network: NetworkKind, consent: CellularConsent | None, key: EpisodeKey) -> None:
    """New transfers only; what the player has buffered keeps playing."""
    if stream_allowed(network, consent, key):
        return
    if network is NetworkKind.OFFLINE:
        raise PlaybackBlockedError("no network").coded("network.offline")
    raise PlaybackBlockedError("mobile data needs consent for this episode").coded(
        "network.cellular_consent_required", series_id=key.series_id, number=key.number
    )


def choose_variant(stream: Stream, criteria: DownloadCriteria, keep: VariantIdentity | None = None) -> StreamVariant:
    """The kept variant if the stream still has it, else the criteria; never the extractor's default past a limit."""
    if keep is not None:
        same = next((v for v in stream.variants if variant_identity(v) == keep), None)
        if same is not None:
            return same
    chosen = match_variant(stream, criteria.quality)
    if isinstance(chosen, NoMatch):
        raise no_match(chosen.reason)
    return chosen


class PlaybackService:
    def __init__(self, state: LocalState, store: FileStore | None) -> None:
        self._state = state
        self._store = store

    def local_copies(self, key: EpisodeKey, criteria: DownloadCriteria) -> tuple[OfflineItem | None, tuple[OfflineItem, ...]]:
        """The newest available copy that fits the criteria, and the other available copies, offered only as such."""
        available = [i for i in self._state.library.for_episode(key) if i.status is ItemStatus.AVAILABLE]
        fitting = [i for i in available if item_matches(i.spec, criteria)]
        chosen = fitting[-1] if fitting else None
        return chosen, tuple(i for i in available if i is not chosen)

    def local(self, key: EpisodeKey, criteria: DownloadCriteria) -> LocalPlayback | None:
        chosen, others = self.local_copies(key, criteria)
        if chosen is None or self._store is None:
            return None
        return LocalPlayback(chosen, self._store.playback_target(chosen.spec.locator), others)

    def online(
        self,
        session: EpisodeSession,
        criteria: DownloadCriteria,
        *,
        online_id: str | None = None,
        cancel: CancelToken = NEVER,
    ) -> OnlinePlayback:
        """An explicit ``online_id`` is the viewer's choice and is played even if it does not fit the criteria."""
        entries = list(session.sources)
        if online_id is not None:
            chosen = _entry(entries, online_id)
            candidates: Sequence[PlayerEntry] = ()
            policy = FallbackPolicy.SINGLE
        else:
            matching = session.rank([e for e in entries if match_source(e, criteria)])
            if not matching:
                reason = no_match_reason(entries, criteria)
                raise no_match(reason.reason if reason else "audio")
            chosen, candidates, policy = matching[0], matching, FallbackPolicy.WALK
        resolved = _resolve(lambda: session.resolve(chosen, candidates=candidates, policy=policy, cancel=cancel))
        variant = choose_variant(resolved.stream, criteria)
        selection = PlaybackSelection(resolved.source.online_id, variant_identity(variant), criteria)
        return OnlinePlayback(resolved.source, resolved.stream, variant, selection, match_source(resolved.source, criteria))

    def refresh(
        self,
        session: EpisodeSession,
        selection: PlaybackSelection,
        *,
        variant: VariantIdentity | None = None,
        cancel: CancelToken = NEVER,
    ) -> OnlinePlayback:
        """The same source resolved afresh; the same variant, or by the kept criteria; ``variant`` is a manual switch."""
        entry = _entry(list(session.sources), selection.online_id)
        resolved = _resolve(lambda: session.resolve(entry, policy=FallbackPolicy.SINGLE, force_fresh=True, cancel=cancel))
        if variant is not None:
            chosen = next((v for v in resolved.stream.variants if variant_identity(v) == variant), None)
            if chosen is None:
                raise no_match("quality")
        else:
            chosen = choose_variant(resolved.stream, selection.criteria, keep=selection.variant)
        identity = variant_identity(chosen)
        kept = PlaybackSelection(selection.online_id, identity, selection.criteria)
        return OnlinePlayback(
            resolved.source,
            resolved.stream,
            chosen,
            kept,
            match_source(resolved.source, selection.criteria),
            variant_changed=identity != selection.variant,
        )


def _entry(entries: Sequence[PlayerEntry], online_id: str) -> PlayerEntry:
    found = next((e for e in entries if e.online_id == online_id), None)
    if found is None:
        raise UnknownRecordError("the chosen source is not offered any more").coded("unknown_source", online_id=online_id)
    return found


def _resolve[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except SourcesExhaustedError as exc:
        if any(a.error in _INTERACTION_ERRORS for a in exc.attempts):
            host = next((a.host for a in exc.attempts if a.host), None)
            online_id = next((a.source.online_id for a in exc.attempts if a.error in _INTERACTION_ERRORS), None)
            raise InteractionRequiredError("the host needs an interaction").coded(
                "interaction.required", scope="host", host=host, online_id=online_id
            ) from None
        raise
