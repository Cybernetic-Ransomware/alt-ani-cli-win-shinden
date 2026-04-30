import html as _html
import json
import re

from selectolax.parser import HTMLParser, Node

from alt_ani_cli.shinden.models import PlayerEntry

_RES_RE = re.compile(r"(\d+)")


def parse_players(html: str) -> list[PlayerEntry]:
    """Extract player list from an episode page.

    Each <a data-episode="..."> button carries a JSON payload with the
    shinden online_id needed to call the API, plus metadata like player
    name, audio language, subtitle language, and max resolution.
    The date_added field is parsed from the .ep-online-added column of the
    containing table row (None when that column is absent).
    """
    tree = HTMLParser(html)
    players: list[PlayerEntry] = []

    for node in tree.css("a[data-episode]"):
        raw = node.attributes.get("data-episode", "")
        if not raw:
            continue
        try:
            data = json.loads(_html.unescape(raw))
        except json.JSONDecodeError, ValueError:
            continue

        online_id = str(data.get("online_id", "")).strip()
        player_name = str(data.get("player", "")).strip()
        if not online_id or not player_name:
            continue

        max_res_raw = str(data.get("max_res", "")).strip()

        date_added = _row_date(node)

        players.append(
            PlayerEntry(
                online_id=online_id,
                player=player_name,
                lang_audio=str(data.get("lang_audio", "")).strip(),
                lang_subs=str(data.get("lang_subs", "")).strip(),
                max_res=max_res_raw or None,
                date_added=date_added,
            )
        )

    return players


def _row_date(node: Node) -> str | None:
    """Walk up the DOM to the enclosing <tr> and extract the .ep-online-added cell text."""
    current = node.parent
    while current is not None and current.tag != "tr":
        current = current.parent
    if current is None:
        return None
    date_cell = current.css_first(".ep-online-added")
    if date_cell is None:
        return None
    text = date_cell.text(strip=True)
    return text or None


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
