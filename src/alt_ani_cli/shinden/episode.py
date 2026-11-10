import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser, Node

from alt_ani_cli import diagnostics
from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.models import PlayerEntry
from alt_ani_cli.shinden.utils import ParseTally, _check_challenge, drift

_RES_RE = re.compile(r"(\d+)")
_PAGE = "players"
_EPISODE_PATH_RE = re.compile(r"/episode/\d+-[A-Za-z0-9-]+/view/\d+")
_SHINDEN_HOST = urlsplit(SHINDEN_BASE).hostname
# keys every 2.5 player record carries, even when their value is null or empty
_CONTRACT_KEYS = ("audio", "sub", "res", "group")


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
    return list(read_players(HTMLParser(html)).players)


def read_players(tree: HTMLParser) -> EpisodePlayers:
    """The JSON island is the only source of players; the DOM may only reject the page, never add an id."""
    _check_challenge(tree, _PAGE)
    data = _player_data(tree)
    visible_rows, hidden_rows = _player_rows(tree)
    trap = _trap_id(data, hidden_rows)

    tally = ParseTally(_PAGE)
    players: list[PlayerEntry] = []
    json_ids: set[str] = set()
    missing_keys: set[str] = set()
    degraded = 0
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
        missing = {key for key in _CONTRACT_KEYS if key not in raw}
        missing_keys |= missing
        degraded += bool(missing)

    valid_ids = frozenset(p.online_id for p in players)
    tally.finish()
    _check_dom_consistency(visible_rows, json_ids, valid_ids, trap)
    if missing_keys:
        diagnostics.shinden_players_degraded(missing=",".join(sorted(missing_keys)), affected=degraded)
    return EpisodePlayers(players=tuple(players), player_ids=valid_ids, trap_id=trap, xhr_endpoint=data.get("xhr"))


def _player_data(tree: HTMLParser) -> dict:
    script = tree.css_first("script#episode-players-data")
    if script is None:
        raise drift(_PAGE, "player data script#episode-players-data not found")
    try:
        data = json.loads(script.text())
    except json.JSONDecodeError as exc:
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
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
        return str(int(value))
    return None


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


def _player_rows(tree: HTMLParser) -> tuple[list[str | None], list[str | None]]:
    """Normalised ids of visible and hidden player rows, ``None`` for a row without a valid id."""
    rows = [*tree.css("li.ep-pl-row"), *tree.css("[data-online-id]:not(li.ep-pl-row)")]
    visible: list[str | None] = []
    hidden: list[str | None] = []
    for node in rows:
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


_AUDIO_RANK_WATCH = {"pl": 3, "jp": 2, "en": 1}  # PL dub first for interactive
_AUDIO_RANK_DOWNLOAD = {"jp": 3, "en": 2, "pl": 1}  # JP original first for archiving


def _audio_rank(lang: str, download: bool = False) -> int:
    table = _AUDIO_RANK_DOWNLOAD if download else _AUDIO_RANK_WATCH
    return table.get(lang.lower(), 0)


def _subs_rank(lang: str) -> int:
    return {"pl": 2, "en": 1}.get(lang.lower(), 0)


def _res_rank(res: str | None) -> int:
    if not res:
        return 0
    m = _RES_RE.search(res)
    return int(m.group(1)) if m else 0


def sort_players(players: list[PlayerEntry], *, download: bool = False) -> list[PlayerEntry]:
    """Sort players: lang_audio DESC → lang_subs DESC → max_res DESC → date_added DESC.

    In download mode audio priority is jp > en > pl (original first).
    In watch mode it is pl > jp > en (dub first for interactive use).
    """
    return sorted(
        players,
        key=lambda p: (
            _audio_rank(p.lang_audio, download=download),
            _subs_rank(p.lang_subs),
            _res_rank(p.max_res),
            p.date_added or "",
        ),
        reverse=True,
    )
