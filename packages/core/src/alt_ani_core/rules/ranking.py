import re

from alt_ani_core.models import PlayerEntry

_RES_RE = re.compile(r"(\d+)")

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
