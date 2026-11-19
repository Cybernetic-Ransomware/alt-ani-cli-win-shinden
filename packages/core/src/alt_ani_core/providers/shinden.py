"""shinden.pl behind the provider contract; a thin adapter over the unchanged shinden package."""

import re
from pathlib import Path

from alt_ani_core import diagnostics
from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import ShindenError, ShindenXhrError
from alt_ani_core.events import EventSink, XhrFallback
from alt_ani_core.models import EmbedURL, EpisodeListing, EpisodeRow, PlayerEntry, SearchResults, SeriesMetadata, SeriesRef
from alt_ani_core.providers.api import url_matches
from alt_ani_core.rules import xhr as xhr_rules
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.shinden import metadata as shinden_metadata
from alt_ani_core.shinden import search as shinden_search
from alt_ani_core.shinden import series as shinden_series
from alt_ani_core.shinden import xhr as shinden_xhr
from alt_ani_core.shinden.xhr import EpisodeXhr
from alt_ani_core.transport import clearance
from alt_ani_core.transport.api import HttpSession, SessionProfile

_HOSTS = frozenset({"shinden.pl", "www.shinden.pl"})
_PATH_RE = re.compile(r"/(?:(?:series|titles)/\d+-[^/]+|episode/[^/]+)(?:/|$)")


class ShindenEmbeds:
    """Embeds come from the player API one XHR at a time; the episode page's token is refreshed at most once per operation."""

    def __init__(self, xhr: EpisodeXhr, *, min_sleep_seconds: float) -> None:
        self.antibot_phase = True
        self.xhr = xhr
        self._min_sleep_seconds = min_sleep_seconds

    def begin_operation(self) -> None:
        self.xhr.begin_operation()

    def embed(self, client: HttpSession, online_id: str, *, cancel: CancelToken) -> EmbedURL:
        return shinden_api.resolve_embed(
            client, online_id, xhr=self.xhr, min_sleep_seconds=self._min_sleep_seconds, cancel=cancel
        )

    def stops_operation(self, exc: ShindenError) -> bool:
        return isinstance(exc, ShindenXhrError) and xhr_rules.xhr_outcome(exc) is not xhr_rules.XhrOutcome.NEXT_PLAYER

    def diag(self) -> dict:
        xhr = self.xhr
        return {"xhr_backend": xhr.backend, "token_refreshed": xhr.refresh_used, "xhr_fallback": xhr.fallback_reason}


class ShindenProvider:
    name = "shinden"

    def __init__(self, *, xhr_backend: str, antibot_delay_sec: float, events: EventSink) -> None:
        self._xhr_backend = shinden_xhr.parse_backend(xhr_backend)
        self._antibot_delay_sec = antibot_delay_sec
        self._events = events

    def owns(self, url: str) -> bool:
        return url_matches(url, hosts=_HOSTS, path=_PATH_RE)

    def session_profile(self, access: AccessConfig, cache_dir: Path) -> SessionProfile:
        return clearance.shinden_profile(clearance.get_clearance(access, cache_dir, self._events))

    def search(self, client: HttpSession, query: str, *, cancel: CancelToken) -> SearchResults:
        return shinden_search.search_series(client, query, cancel=cancel)

    def series_from_url(self, url: str) -> SeriesRef:
        return shinden_series.parse_series_url(url)

    def metadata(self, client: HttpSession, ref: SeriesRef) -> SeriesMetadata:
        return shinden_metadata.fetch_series_metadata(client, ref)

    def episodes(self, client: HttpSession, ref: SeriesRef, *, cancel: CancelToken) -> EpisodeListing:
        return shinden_series.list_episodes(client, ref, cancel=cancel)

    def load_episode(self, client: HttpSession, episode: EpisodeRow) -> tuple[list[PlayerEntry], ShindenEmbeds]:
        players, xhr = shinden_xhr.load_episode(client, episode.url, backend=self._xhr_backend)
        if xhr.forced:
            diagnostics.shinden_xhr_context(action="forced", backend=xhr.backend, reason=None)
        elif xhr.fallback_reason is not None and players:
            diagnostics.shinden_xhr_context(action="fallback", backend=xhr.backend, reason=xhr.fallback_reason)
            self._events.emit(XhrFallback(episode=episode.number, reason=xhr.fallback_reason))
        return players, ShindenEmbeds(xhr, min_sleep_seconds=self._antibot_delay_sec)
