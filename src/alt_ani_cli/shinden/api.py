import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import NoReturn
from urllib.parse import quote, urlsplit

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from selectolax.parser import HTMLParser

from alt_ani_cli import diagnostics
from alt_ani_cli.config import (
    ANTIBOT_DELAY_SEC,
    GUEST_AUTH,
    MAX_XHR_WAIT_SEC,
    SHINDEN_API_BASE,
    SHINDEN_BASE,
)
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.errors import ShindenError, ShindenXhrError
from alt_ani_cli.failure import CATEGORY_NO_STREAM_URL, CATEGORY_PARSER_DRIFT, classify_exception
from alt_ani_cli.models import EmbedURL
from alt_ani_cli.shinden import xhr as shinden_xhr
from alt_ani_cli.shinden.xhr import BACKEND_API4, BACKEND_NEW, EpisodeXhr

_API = EXCEPTIONS["api"]

STAGE_LOAD = "player_load"
STAGE_SHOW = "player_show"
STAGE_REFRESH = "token_refresh"

CATEGORY_PLAYERS_CHANGED = "player_list_changed"
_PLAYERS_CHANGED_REASONS = frozenset({"player_not_listed", "player_is_honeypot"})

# Protection and auth states: the API refuses this client, not this player.
ACCESS_BLOCK_CODES = frozenset({"turnstile", "auth_required", "invalid_token", "konto_robot", "vpnp_limit", "ochrona_godz"})

_WAIT_TEXT_RE = re.compile(r"\d{1,6}")
_API_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_TRANSPORT_CATEGORIES = frozenset({"timeout", "network_error"})


class XhrOutcome(StrEnum):
    NEXT_PLAYER = "next_player"
    ACCESS_BLOCKED = "access_blocked"
    SERVICE_UNAVAILABLE = "service_unavailable"
    API_PROTOCOL_ERROR = "api_protocol_error"
    PLAYERS_CHANGED = "players_changed"


def xhr_outcome(err: ShindenXhrError) -> XhrOutcome:
    """Fallback policy only; classification already happened when ``err`` was raised."""
    status = err.http_status
    if err.api_code in ACCESS_BLOCK_CODES or status in (401, 403, 429):
        return XhrOutcome.ACCESS_BLOCKED
    if err.category in _TRANSPORT_CATEGORIES or (status is not None and status >= 500):
        return XhrOutcome.SERVICE_UNAVAILABLE
    if err.reason in _PLAYERS_CHANGED_REASONS:
        return XhrOutcome.PLAYERS_CHANGED
    if err.stage == STAGE_REFRESH:
        # the episode page backs every player, so a refresh failure (even a 404) is never one player's
        return XhrOutcome.API_PROTOCOL_ERROR
    if err.category == CATEGORY_NO_STREAM_URL or status in (404, 410):
        return XhrOutcome.NEXT_PLAYER
    # no api_code is known to be source-specific, so an unrecognised one stops rather than walks on
    return XhrOutcome.API_PROTOCOL_ERROR


def _detail(err: ShindenXhrError) -> str:
    parts = []
    if err.http_status is not None and err.category == "http_error":
        parts.append(f"HTTP {err.http_status}")
    if err.api_code is not None:
        parts.append(f"code={err.api_code}")
    if err.category in _TRANSPORT_CATEGORIES:
        parts.append(err.category)
    elif err.reason not in ("http_status", "api_error"):
        parts.append(f"reason={err.reason}")
    return ", ".join(parts)


def stop_message(err: ShindenXhrError) -> str:
    """User-facing text built from local templates only — never the server's error.message."""
    key = {
        XhrOutcome.ACCESS_BLOCKED: "xhr_access_blocked",
        XhrOutcome.SERVICE_UNAVAILABLE: "xhr_unavailable",
        XhrOutcome.PLAYERS_CHANGED: "xhr_players_changed",
    }.get(xhr_outcome(err), "xhr_protocol")
    msg = _API[key].format(stage=err.stage, detail=_detail(err))
    if err.retry_after is not None:
        msg = f"{msg} {_API['retry_after_hint'].format(seconds=err.retry_after)}"
    return msg


def _xhr_error(online_id: str, *, stage: str, category: str, reason: str, **fields) -> ShindenXhrError:
    err = ShindenXhrError("", stage=stage, category=category, reason=reason, **fields)
    err.args = (_API["xhr_failed"].format(stage=stage, online_id=repr(online_id), detail=_detail(err)),)
    return err


def _error_payload(text: str) -> dict | None:
    if not text.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, dict) else None


def _api_code(payload: dict | None) -> str | None:
    code = payload.get("code") if payload else None
    return code if isinstance(code, str) and _API_CODE_RE.fullmatch(code) else None


def _seconds(value: object) -> int | None:
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return math.ceil(value)


def _retry_after(resp, payload: dict | None) -> int | None:
    header = resp.headers.get("Retry-After") if resp.headers is not None else None
    if isinstance(header, str):
        seconds = _seconds(header)
        if seconds is not None:
            return seconds
        try:
            when = parsedate_to_datetime(header)
        except TypeError, ValueError:
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0, math.ceil((when - datetime.now(UTC)).total_seconds()))
    return _seconds(payload.get("retry_after")) if payload else None


def _send(client: cffi_requests.Session, url: str):
    """The response, or the transport category: the curl exception, whose text carries the auth URL, dies here."""
    try:
        return client.get(url)
    except CurlRequestException as exc:
        return classify_exception(exc)


def _get(client: cffi_requests.Session, url: str, online_id: str, stage: str, backend: str) -> str:
    resp = _send(client, url)
    if isinstance(resp, str):
        raise _xhr_error(online_id, stage=stage, category=resp, reason="transport", backend=backend)

    text = resp.text if isinstance(resp.text, str) else ""
    payload = _error_payload(text)
    if resp.status_code < 400 and payload is None:
        return text
    raise _xhr_error(
        online_id,
        stage=stage,
        category="http_error",
        reason="api_error" if payload is not None else "http_status",
        http_status=resp.status_code,
        api_code=_api_code(payload),
        retry_after=_retry_after(resp, payload),
        backend=backend,
    )


def _wait_reason(body: str) -> tuple[int | None, str | None]:
    if _WAIT_TEXT_RE.fullmatch(body):
        parsed: object = int(body)
    else:
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
    if isinstance(parsed, bool) or not isinstance(parsed, int | float):
        return None, "wait_not_a_number" if body else "wait_missing"
    if isinstance(parsed, float) and not math.isfinite(parsed):
        return None, "wait_not_a_number"
    if isinstance(parsed, float) and not parsed.is_integer():
        return None, "wait_not_whole_seconds"
    if parsed < 0:
        return None, "wait_negative"
    if parsed > MAX_XHR_WAIT_SEC:
        return None, "wait_above_limit"
    return int(parsed), None


def _wait_seconds(text: str, online_id: str, backend: str) -> int:
    """Whole seconds only, as bare digits or a JSON number; anything else is contract drift, never a guess."""
    value, reason = _wait_reason(text.strip())
    if value is None:
        raise _xhr_error(
            online_id,
            stage=STAGE_LOAD,
            category=CATEGORY_PARSER_DRIFT,
            reason=reason or "wait_missing",
            http_status=200,
            backend=backend,
        )
    return value


def _is_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


@dataclass(frozen=True)
class _Target:
    base: str
    auth_query: str = field(repr=False)
    backend: str = BACKEND_API4


_API4_TARGET = _Target(base=f"{SHINDEN_API_BASE}/xhr", auth_query=f"auth={GUEST_AUTH}")


def _target(xhr: EpisodeXhr) -> _Target:
    if xhr.backend != BACKEND_NEW or xhr.context is None:
        return _API4_TARGET
    # GUEST_AUTH is stored pre-encoded; the page token is raw and is encoded here and nowhere else
    return _Target(base=xhr.context.endpoint, auth_query="auth=" + quote(xhr.context.auth, safe=""), backend=BACKEND_NEW)


def resolve_embed(
    client: cffi_requests.Session,
    online_id: str,
    *,
    xhr: EpisodeXhr,
    min_sleep_seconds: float = ANTIBOT_DELAY_SEC,
) -> EmbedURL:
    """Only invalid_token on the new backend earns one page refresh per operation; nothing ever retries on api4."""
    _check_listed(xhr, online_id)
    try:
        return _resolve_once(client, online_id, _target(xhr), min_sleep_seconds)
    except ShindenXhrError as exc:
        if exc.backend != BACKEND_NEW or exc.api_code != "invalid_token" or xhr.refresh_used:
            raise
    _refresh(client, xhr, online_id)
    return _resolve_once(client, online_id, _target(xhr), min_sleep_seconds)


def _check_listed(xhr: EpisodeXhr, online_id: str) -> None:
    """The honeypot never gets a request; after a refresh, neither does a player missing from the current list."""
    if online_id in xhr.player_ids and online_id != xhr.trap_id:
        return
    if not xhr.list_refreshed:
        raise AssertionError("player is not on this episode page")
    reason = "player_is_honeypot" if online_id == xhr.trap_id else "player_not_listed"
    raise _xhr_error(online_id, stage=STAGE_REFRESH, category=CATEGORY_PLAYERS_CHANGED, reason=reason, backend=xhr.backend)


def _refresh(client: cffi_requests.Session, xhr: EpisodeXhr, online_id: str) -> None:
    xhr.refresh_used = True
    resp = _send(client, xhr.episode_url)
    if isinstance(resp, str):
        _refresh_failed(xhr, online_id, category=resp, reason="transport")
    if resp.status_code >= 400:
        _refresh_failed(xhr, online_id, category="http_error", reason="http_status", http_status=resp.status_code)

    fresh = _read_page(resp.text if isinstance(resp.text, str) else "", xhr.episode_url)
    if fresh is None:
        _refresh_failed(xhr, online_id, category=CATEGORY_PARSER_DRIFT, reason="refresh_page_invalid")
    if fresh.context is None:
        # no api4 here: the new backend has already answered for this episode
        _refresh_failed(xhr, online_id, category=CATEGORY_PARSER_DRIFT, reason=f"refresh_{fresh.fallback_reason}")

    xhr.context = fresh.context
    xhr.player_ids = fresh.player_ids
    xhr.trap_id = fresh.trap_id
    xhr.list_refreshed = True
    diagnostics.shinden_xhr_context(action="token_refresh", backend=xhr.backend, reason=None, online_id=online_id)
    _check_listed(xhr, online_id)


def _read_page(html: str, episode_url: str) -> EpisodeXhr | None:
    try:
        return shinden_xhr.read_episode(html, episode_url, backend=BACKEND_NEW)[1]
    except ShindenError:
        return None


def _refresh_failed(xhr: EpisodeXhr, online_id: str, *, category: str, reason: str, http_status: int | None = None) -> NoReturn:
    diagnostics.shinden_xhr_context(action="token_refresh", backend=xhr.backend, reason=reason, online_id=online_id)
    raise _xhr_error(
        online_id, stage=STAGE_REFRESH, category=category, reason=reason, http_status=http_status, backend=xhr.backend
    )


def _resolve_once(client: cffi_requests.Session, online_id: str, target: _Target, min_sleep_seconds: float) -> EmbedURL:
    """player_load's body is the server-enforced wait; calling player_show sooner returns an empty response."""
    base = f"{target.base}/{online_id}"
    backend = target.backend

    load = _get(client, f"{base}/player_load?{target.auth_query}", online_id, STAGE_LOAD, backend)
    time.sleep(max(_wait_seconds(load, online_id, backend), min_sleep_seconds))

    text = _get(client, f"{base}/player_show?{target.auth_query}&width=0&height=-1", online_id, STAGE_SHOW, backend)

    def no_stream(reason: str, message: str) -> ShindenXhrError:
        return ShindenXhrError(
            message, stage=STAGE_SHOW, category=CATEGORY_NO_STREAM_URL, reason=reason, http_status=200, backend=backend
        )

    iframe = HTMLParser(text).css_first("iframe")
    if not iframe:
        raise no_stream("no_iframe", _API["no_iframe"].format(online_id=repr(online_id), length=len(text)))

    src = iframe.attributes.get("src")
    if not src:
        raise no_stream("iframe_no_src", _API["iframe_no_src"].format(online_id=repr(online_id)))
    if src.startswith("//"):
        src = "https:" + src
    # the src is untrusted server data and may carry tokens, so it never reaches the message
    if not _is_web_url(src):
        raise no_stream("unexpected_iframe_src", _API["unexpected_iframe_src"].format(online_id=repr(online_id)))

    return EmbedURL(url=src, referer=f"{SHINDEN_BASE}/")
