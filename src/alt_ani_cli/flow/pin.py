"""Session-only download source pin: a player identity that survives across episodes of one batch."""

from dataclasses import dataclass
from urllib.parse import urlparse

from alt_ani_cli.models import PlayerEntry


@dataclass(frozen=True)
class PlayerFingerprint:
    """Episode-independent player identity; online_id and date_added change between episodes, so they are excluded."""

    player: str
    lang_audio: str
    lang_subs: str
    max_res: str
    subs_author: str
    source: str


def _norm_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def source_identity(source: str | None) -> str:
    """Hostname without www. for URLs (scheme, path, query and fragment ignored); normalized text otherwise."""
    text = (source or "").strip()
    try:
        host = urlparse(text).hostname
    except ValueError:
        host = None
    if host:
        return host.casefold().removeprefix("www.")
    return _norm_text(text)


def player_fingerprint(p: PlayerEntry) -> PlayerFingerprint:
    return PlayerFingerprint(
        player=_norm_text(p.player),
        lang_audio=_norm_text(p.lang_audio),
        lang_subs=_norm_text(p.lang_subs),
        max_res=_norm_text(p.max_res),
        subs_author=_norm_text(p.subs_author),
        source=source_identity(p.source),
    )


def find_pinned(players: list[PlayerEntry], pin: PlayerFingerprint) -> PlayerEntry | None:
    """First player matching the pin, in the given (already sorted and filtered) order."""
    return next((p for p in players if player_fingerprint(p) == pin), None)
