from alt_ani_core.models import PlayerEntry, SourceFilter


def filter_players(players: list[PlayerEntry], flt: SourceFilter) -> tuple[list[PlayerEntry], list[str]]:
    """Strict AND filter. Returns (filtered, unmatched).

    unmatched lists criteria (e.g. "--lang=pl") that eliminated all candidates;
    when non-empty, filtered is [] and the caller decides whether to fall back.
    A criterion that matches nothing does not narrow the pool, so the remaining
    criteria stay individually diagnosable (reporting is order-dependent).
    """
    result = players[:]
    unmatched: list[str] = []
    if flt.player_name:
        filtered = [p for p in result if p.player.lower() == flt.player_name.lower()]
        if filtered:
            result = filtered
        else:
            unmatched.append(f"--player-name={flt.player_name}")
    if flt.lang:
        filtered = [p for p in result if p.lang_audio == flt.lang]
        if filtered:
            result = filtered
        else:
            unmatched.append(f"--lang={flt.lang}")
    if flt.subs:
        filtered = [p for p in result if p.lang_subs == flt.subs]
        if filtered:
            result = filtered
        else:
            unmatched.append(f"--subs={flt.subs}")
    return (result, []) if not unmatched else ([], unmatched)
