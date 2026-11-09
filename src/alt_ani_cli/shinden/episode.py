import json
import re

from selectolax.parser import HTMLParser

from alt_ani_cli import diagnostics
from alt_ani_cli.models import PlayerEntry
from alt_ani_cli.shinden.utils import ParseTally, _check_challenge, drift

_RES_RE = re.compile(r"(\d+)")
_PAGE = "players"


def parse_players(html: str) -> list[PlayerEntry]:
    """Parse the player JSON island; the ``pulapka`` honeypot id is excluded because loading it flags the client."""
    tree = HTMLParser(html)
    _check_challenge(tree, _PAGE)
    script = tree.css_first("script#episode-players-data")
    if script is None:
        raise drift(_PAGE, "player data script#episode-players-data not found")
    try:
        data = json.loads(script.text())
    except json.JSONDecodeError as exc:
        raise drift(_PAGE, "player data is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("players"), list):
        raise drift(_PAGE, "player data has no players list")
    trap_raw = data.get("pulapka")
    if trap_raw is not None and _online_id(trap_raw) is None:
        raise drift(_PAGE, "honeypot id has an unexpected type")
    trap = _online_id(trap_raw) if trap_raw is not None else None

    tally = ParseTally(_PAGE)
    players: list[PlayerEntry] = []
    json_ids: set[str] = set()
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

    _report_dom_mismatch(tree, json_ids, {p.online_id for p in players}, trap)
    tally.finish()
    return players


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


def _report_dom_mismatch(tree: HTMLParser, json_ids: set[str], valid_ids: set[str], trap: str | None) -> None:
    """Compare the visible player rows with the JSON ids; disagreement means one of the two sources drifted."""
    dom_ids = [
        node.attributes.get("data-online-id") or ""
        for node in tree.css("li.ep-pl-row[data-online-id]")
        if "hidden" not in node.attributes
    ]
    visible = set(dom_ids)
    dom_only = len(visible - json_ids)
    json_only = len(valid_ids - visible)
    trap_visible = trap is not None and trap in visible
    dom_duplicates = len(dom_ids) - len(visible)
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
