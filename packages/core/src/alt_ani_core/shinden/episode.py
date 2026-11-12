import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from alt_ani_core import diagnostics
from alt_ani_core.config import SHINDEN_BASE
from alt_ani_core.dom import Document, Node
from alt_ani_core.dom import parse as parse_html
from alt_ani_core.models import PlayerEntry
from alt_ani_core.shinden.utils import ParseTally, _check_challenge, drift

_ASCII_DIGITS_RE = re.compile(r"[0-9]+")
_MAX_ID_DIGITS = 19  # int64 range; also keeps str() of a JSON integer under Python's int/str digit limit
_PAGE = "players"
_EPISODE_PATH_RE = re.compile(r"/episode/\d+-[A-Za-z0-9-]+/view/\d+")
_SHINDEN_HOST = urlsplit(SHINDEN_BASE).hostname
# 2.5 schema keys, null or empty when unset; a key no accepted record carries means the schema changed
_CONTRACT_KEYS = ("audio", "sub", "res")


def episode_path(url: str) -> str | None:
    """The page path alone, for the diagnostics log — only for a plain shinden.pl episode URL without a query."""
    if any(c in url for c in "?#@"):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname != _SHINDEN_HOST or port is not None:
        return None
    return parts.path if _EPISODE_PATH_RE.fullmatch(parts.path) else None


def episode_url_from_path(path: str) -> str | None:
    return f"{SHINDEN_BASE}{path}" if _EPISODE_PATH_RE.fullmatch(path) else None


@dataclass(frozen=True)
class EpisodePlayers:
    """The episode page's player contract read once; the XHR token is deliberately not part of it."""

    players: tuple[PlayerEntry, ...]
    player_ids: frozenset[str]
    trap_id: str | None
    xhr_endpoint: object


def parse_players(html: str) -> list[PlayerEntry]:
    return list(read_players(parse_html(html)).players)


def read_players(tree: Document) -> EpisodePlayers:
    """The JSON island is the only source of players; the DOM may only reject the page, never add an id."""
    _check_challenge(tree, _PAGE)
    data = _player_data(tree)
    visible_rows, hidden_rows = _player_rows(tree)
    trap = _trap_id(data, hidden_rows)

    tally = ParseTally(_PAGE)
    players: list[PlayerEntry] = []
    json_ids: set[str] = set()
    present_keys: set[str] = set()
    for raw in data["players"]:
        online_id = _online_id(raw.get("online_id")) if isinstance(raw, dict) else None
        if online_id is None:
            tally.reject("bad_online_id")
            continue
        first_seen = online_id not in json_ids
        json_ids.add(online_id)
        if online_id == trap:
            tally.exclude("honeypot")
            continue
        if not first_seen:
            tally.exclude("duplicate")
            continue
        entry = _to_entry(online_id, raw)
        if isinstance(entry, str):
            tally.reject(entry)
            continue
        tally.accept()
        players.append(entry)
        present_keys.update(key for key in _CONTRACT_KEYS if key in raw)

    valid_ids = frozenset(p.online_id for p in players)
    tally.finish()
    _check_dom_consistency(visible_rows, json_ids, valid_ids, trap)
    missing_keys = [key for key in _CONTRACT_KEYS if key not in present_keys]
    if players and missing_keys:
        diagnostics.shinden_players_degraded(missing=",".join(sorted(missing_keys)), affected=len(players))
    return EpisodePlayers(players=tuple(players), player_ids=valid_ids, trap_id=trap, xhr_endpoint=data.get("xhr"))


def _player_data(tree: Document) -> dict:
    script = tree.css_first("script#episode-players-data")
    if script is None:
        raise drift(_PAGE, "player data script#episode-players-data not found")
    try:
        data = json.loads(script.text())
    except ValueError as exc:  # JSONDecodeError, or an integer literal over Python's int/str digit limit
        raise drift(_PAGE, "player data is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("players"), list):
        raise drift(_PAGE, "player data has no players list")
    return data


def _trap_id(data: dict, hidden_rows: list[str | None]) -> str | None:
    """Loading the honeypot flags the client, so a missing ``pulapka`` key is drift, never proof that there is none."""
    if "pulapka" not in data:
        raise drift(_PAGE, "honeypot declaration is missing")
    trap_raw = data["pulapka"]
    trap = None if trap_raw is None else _online_id(trap_raw)
    if trap_raw is not None and trap is None:
        raise drift(_PAGE, "honeypot id has an unexpected type")
    if any(row_id is None or row_id != trap for row_id in hidden_rows):
        raise drift(_PAGE, "hidden player row does not match the honeypot id")
    return trap


def _online_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        digits = str(value) if 0 < value < 10**_MAX_ID_DIGITS else ""
    elif isinstance(value, str) and _ASCII_DIGITS_RE.fullmatch(value.strip()):
        digits = value.strip().lstrip("0")
    else:
        return None
    return digits if 0 < len(digits) <= _MAX_ID_DIGITS else None


def _opt_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _to_entry(online_id: str, raw: dict) -> PlayerEntry | str:
    """Build a PlayerEntry, or return the rejection reason."""
    player = raw.get("player")
    if not isinstance(player, str) or not player.strip():
        return "bad_player"
    audio, subs = raw.get("audio"), raw.get("sub")
    if not isinstance(audio, str | None) or not isinstance(subs, str | None):
        return "bad_lang"
    res = raw.get("res")
    max_res = f"{res}p" if isinstance(res, int) and not isinstance(res, bool) and res > 0 else None
    raw_group = raw.get("group")
    group: dict = raw_group if isinstance(raw_group, dict) else {}
    return PlayerEntry(
        online_id=online_id,
        player=player.strip(),
        lang_audio=(audio or "").strip(),
        lang_subs=(subs or "").strip(),
        max_res=max_res,
        # Same meaning as the pre-2.5 payload: subtitle group name and the group's own website.
        subs_author=_opt_text(group.get("name")),
        source=_opt_text(group.get("url")),
    )


def _is_hidden(node: Node) -> bool:
    return "hidden" in node.attributes or (node.attributes.get("aria-hidden") or "").strip().lower() == "true"


def _player_rows(tree: Document) -> tuple[list[str | None], list[str | None]]:
    """Normalised ids of visible and hidden rows inside the player list, ``None`` for a row without a valid id."""
    rows: dict[int, Node] = {}
    for container in tree.css(".ep-pl-list"):
        # selector lists and nested lists repeat elements; Node hashes by identity (mem_id) but == compares markup
        for node in container.css("li.ep-pl-row, [data-online-id]"):
            rows.setdefault(hash(node), node)
    visible: list[str | None] = []
    hidden: list[str | None] = []
    for node in rows.values():
        (hidden if _is_hidden(node) else visible).append(_online_id(node.attributes.get("data-online-id")))
    return visible, hidden


def _check_dom_consistency(
    visible_rows: list[str | None], json_ids: set[str], valid_ids: frozenset[str], trap: str | None
) -> None:
    """Visible rows with no valid JSON player is drift; any other disagreement with working JSON is only reported."""
    if not valid_ids and any(row_id is None or row_id != trap for row_id in visible_rows):
        raise drift(_PAGE, f"no valid player in data while {len(visible_rows)} player rows are visible")
    visible = {row_id for row_id in visible_rows if row_id is not None}
    dom_only = sum(1 for row_id in visible_rows if row_id is None) + len(visible - json_ids)
    json_only = len(valid_ids - visible)
    trap_visible = trap is not None and trap in visible
    dom_duplicates = sum(1 for row_id in visible_rows if row_id is not None) - len(visible)
    if dom_only or json_only or trap_visible or dom_duplicates:
        diagnostics.shinden_players_mismatch(dom_only, json_only, trap_visible, dom_duplicates)
