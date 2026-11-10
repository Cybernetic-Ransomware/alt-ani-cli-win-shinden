"""Per-episode XHR state: which backend serves player_load/player_show, and the episode page's signed token."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests
from selectolax.parser import HTMLParser

from alt_ani_cli.config import SHINDEN_BASE, SHINDEN_XHR_BASE
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.models import PlayerEntry
from alt_ani_cli.shinden import episode as shinden_episode
from alt_ani_cli.shinden.utils import _check_age_gate

BACKEND_NEW = "new_xhr"
BACKEND_API4 = "api4"

_BACKEND_CHOICES = {"new": BACKEND_NEW, "api4": BACKEND_API4}

# Base64 as served by Shinden 2.5 (verified 2026-11-10). No "%", so the raw value is URL-encoded exactly once.
_AUTH_RE = re.compile(r"[A-Za-z0-9+/=_-]{16,4096}")
_SHINDEN_HOST = urlsplit(SHINDEN_BASE).hostname


def parse_backend(value: str) -> str:
    backend = _BACKEND_CHOICES.get(value.strip().lower())
    if backend is None:
        raise ValueError(EXCEPTIONS["cli"]["invalid_xhr_backend"].format(value=repr(value)))
    return backend


@dataclass(frozen=True)
class EpisodeXhrContext:
    """The only object holding the token; never persisted, logged or shared with another episode."""

    endpoint: str
    auth: str = field(repr=False)


@dataclass
class EpisodeXhr:
    """Built anew for every episode page and never persisted."""

    episode_url: str
    player_ids: frozenset[str]
    trap_id: str | None
    backend: str
    context: EpisodeXhrContext | None = None
    fallback_reason: str | None = None
    forced: bool = False
    refresh_used: bool = False
    # set once a token refresh has replaced the player list read when the episode was opened
    list_refreshed: bool = False

    def begin_operation(self) -> None:
        self.refresh_used = False


def load_episode(client: cffi_requests.Session, episode_url: str, *, backend: str) -> tuple[list[PlayerEntry], EpisodeXhr]:
    resp = client.get(episode_url)
    resp.raise_for_status()
    return read_episode(resp.text, episode_url, backend=backend)


def read_episode(html: str, episode_url: str, *, backend: str) -> tuple[list[PlayerEntry], EpisodeXhr]:
    """Page checks and the player list come first, so a protection page or broken HTML is never read as a missing token."""
    _check_age_gate(html)
    tree = HTMLParser(html)
    page = shinden_episode.read_players(tree)

    xhr = EpisodeXhr(
        episode_url=episode_url,
        player_ids=page.player_ids,
        trap_id=page.trap_id,
        backend=BACKEND_API4,
        forced=backend == BACKEND_API4,
    )
    if backend == BACKEND_NEW:
        context, reason = _context(tree, page.xhr_endpoint)
        if context is not None:
            xhr.backend, xhr.context = BACKEND_NEW, context
        else:
            xhr.fallback_reason = reason
    return list(page.players), xhr


def _context(tree: HTMLParser, endpoint: object) -> tuple[EpisodeXhrContext | None, str | None]:
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None, "endpoint_missing"
    if not _is_shinden_xhr(endpoint):
        return None, "endpoint_rejected"
    island = tree.css_first("div.ep-players__island[data-player-auth]")
    auth = island.attributes.get("data-player-auth") if island is not None else None
    if not auth:
        return None, "auth_missing"
    if not _AUTH_RE.fullmatch(auth):
        return None, "auth_malformed"
    return EpisodeXhrContext(endpoint=SHINDEN_XHR_BASE, auth=auth), None


def _is_shinden_xhr(value: str) -> bool:
    """The page only confirms the endpoint; the request always goes to SHINDEN_XHR_BASE, never to a page-chosen host."""
    if not value.startswith(("//", "https://")) or any(c.isspace() or c in "\\?#" for c in value):
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.hostname == _SHINDEN_HOST
        and parts.username is None
        and parts.password is None
        and port is None
        and parts.path.rstrip("/") == "/xhr"
    )
