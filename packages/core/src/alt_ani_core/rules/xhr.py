"""How a failed Shinden player API call ends the operation, and its user-facing text; reads only the error's primitives."""

from enum import StrEnum

from alt_ani_core.errors import ShindenXhrError
from alt_ani_core.messages import MESSAGES
from alt_ani_core.rules.failure import CATEGORY_NO_STREAM_URL

_API = MESSAGES["api"]

STAGE_LOAD = "player_load"
STAGE_SHOW = "player_show"
STAGE_REFRESH = "token_refresh"

_PLAYERS_CHANGED_REASONS = frozenset({"player_not_listed", "player_is_honeypot"})

# Protection and auth states: the API refuses this client, not this player.
ACCESS_BLOCK_CODES = frozenset({"turnstile", "auth_required", "invalid_token", "konto_robot", "vpnp_limit", "ochrona_godz"})

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


def xhr_detail(err: ShindenXhrError) -> str:
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
        XhrOutcome.NEXT_PLAYER: "xhr_next_player",
    }.get(xhr_outcome(err), "xhr_protocol")
    msg = _API[key].format(stage=err.stage, detail=xhr_detail(err))
    if err.retry_after is not None:
        msg = f"{msg} {_API['retry_after_hint'].format(seconds=err.retry_after)}"
    return msg
