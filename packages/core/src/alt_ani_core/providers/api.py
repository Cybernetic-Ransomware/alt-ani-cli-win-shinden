"""What the engine needs from one anime site; everything after the embed URL (extraction, ranking, health) is shared."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import ShindenError
from alt_ani_core.models import EmbedURL, EpisodeListing, EpisodeRow, PlayerEntry, SearchResults, SeriesMetadata, SeriesRef
from alt_ani_core.transport.api import HttpSession, SessionProfile

_SCHEMES = frozenset({"http", "https"})
_DOT_SEGMENTS = frozenset({".", ".."})


class EpisodeEmbeds(Protocol):
    """How one opened episode turns a listed player into an embed URL; its EpisodeSession serializes every call."""

    # whether a fresh embed is the slow, rate-limited step the session reports as the anti-bot phase
    antibot_phase: bool

    def begin_operation(self) -> None:
        """Called once per session operation, before any embed; resets per-operation budgets such as a token refresh."""
        ...

    def embed(self, client: HttpSession, online_id: str, *, cancel: CancelToken) -> EmbedURL: ...

    def stops_operation(self, exc: ShindenError) -> bool:
        """True when a failed embed means the site's shared API failed, so no further source is tried."""
        ...

    def diag(self) -> dict: ...


class Provider(Protocol):
    """One site: its HTTP session, catalogue and episode pages; the engine owns sessions, generations and cancellation."""

    name: str

    def owns(self, url: str) -> bool:
        """Whether a series or episode URL belongs to this site; routing uses nothing else."""
        ...

    def session_profile(self, access: AccessConfig, cache_dir: Path) -> SessionProfile: ...

    def search(self, client: HttpSession, query: str, *, cancel: CancelToken) -> SearchResults: ...

    def series_from_url(self, url: str) -> SeriesRef: ...

    def metadata(self, client: HttpSession, ref: SeriesRef) -> SeriesMetadata: ...

    def episodes(self, client: HttpSession, ref: SeriesRef, *, cancel: CancelToken) -> EpisodeListing: ...

    def load_episode(self, client: HttpSession, episode: EpisodeRow) -> tuple[list[PlayerEntry], EpisodeEmbeds]: ...


class StaticEmbeds:
    """Embeds an episode page lists outright: no request, no anti-bot wait, nothing to refresh."""

    def __init__(self, embeds: Mapping[str, EmbedURL]) -> None:
        self.antibot_phase = False
        self._embeds = dict(embeds)

    def begin_operation(self) -> None:
        pass

    def embed(self, client: HttpSession, online_id: str, *, cancel: CancelToken) -> EmbedURL:
        cancel.raise_if_cancelled()
        embed = self._embeds.get(online_id)
        if embed is None:
            raise AssertionError("player is not on this episode page")
        return embed

    def stops_operation(self, exc: ShindenError) -> bool:
        return False

    def diag(self) -> dict:
        return {}


def url_matches(url: str, *, hosts: frozenset[str], path: re.Pattern[str]) -> bool:
    """An http(s) URL on one of ``hosts`` exactly, whose path starts with ``path``; no credentials, port or dot segments."""
    if any(c.isspace() or c == "\\" for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in _SCHEMES
        and parts.hostname in hosts
        and parts.username is None
        and parts.password is None
        and port is None
        and not _DOT_SEGMENTS.intersection(unquote(parts.path).split("/"))
        and path.match(parts.path) is not None
    )
