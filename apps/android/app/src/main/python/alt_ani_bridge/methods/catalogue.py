"""Contract v1: catalogue, episode sessions and the legacy history; data unchanged, inputs now strictly checked."""

from typing import Any

from alt_ani_bridge import dto
from alt_ani_bridge.context import Call, SessionEntry
from alt_ani_bridge.errors import BridgeError, describe
from alt_ani_bridge.methods import common, method
from alt_ani_core.models import FallbackPolicy, PlayerEntry, Purpose, SourceFilter


def _player(by_id: dict[str, PlayerEntry], online_id: object) -> PlayerEntry:
    player = by_id.get(str(online_id))
    if player is None:
        raise BridgeError("unknown_source", "no such source in this episode session", online_id=str(online_id))
    return player


def sources(session: SessionEntry) -> list[dict[str, Any]]:
    return [dto.player(p) for p in session.session.sources]


def search(c: Call) -> Any:
    hits = c.engine.search(c.r.text("query"), cancel=c.token)
    return {"hits": [dto.series_hit(hit) for hit in hits]}


def metadata(c: Call) -> Any:
    ref = c.engine.series_from_url(c.r.text("url"))
    return {"metadata": dto.series_metadata(c.engine.metadata(ref, cancel=c.token))}


def metadata_many(c: Call) -> Any:
    refs = [c.engine.series_from_url(url) for url in c.r.strs("urls")]
    return {"metadata": [dto.series_metadata(meta) for meta in c.engine.metadata_many(refs, cancel=c.token)]}


def episodes(c: Call) -> Any:
    ref = common.series(c.r) if c.r.has("series") else c.engine.series_from_url(c.r.text("url"))
    return dto.episode_listing(c.engine.episodes(ref, cancel=c.token))


def open_episode(c: Call) -> Any:
    found = c.open_session(common.episode(c.r))
    return {"session_id": found.session_id, "episode": dto.episode(found.session.episode), "sources": sources(found)}


def select(c: Call) -> Any:
    flt = c.r.obj("filter", ("lang", "subs", "player_name"), None)
    session = c.session(c.r.text("session_id")).session
    chosen = SourceFilter(
        lang=flt.text("lang", None, nullable=True) if flt else None,
        subs=flt.text("subs", None, nullable=True) if flt else None,
        player_name=flt.text("player_name", None, nullable=True) if flt else None,
    )
    return dto.selection(session.select(chosen, purpose=c.r.enum("purpose", Purpose, Purpose.WATCH)))


def resolve(c: Call) -> Any:
    session = c.session(c.r.text("session_id")).session
    by_id = {p.online_id: p for p in session.sources}
    chosen = _player(by_id, c.r.text("online_id"))
    candidates = [_player(by_id, online_id) for online_id in c.r.strs("candidate_ids", default=[])]
    policy = c.r.enum("policy", FallbackPolicy, FallbackPolicy.SINGLE)
    if c.r.flag("rank_by_health", False):
        candidates = session.rank(candidates)
    resolved = session.resolve(
        chosen, candidates=candidates, policy=policy, force_fresh=c.r.flag("force_fresh", False), cancel=c.token
    )
    return dto.resolved_stream(resolved, c.r.text("quality", None, nullable=True))


def probe_hosts(c: Call) -> Any:
    session = c.session(c.r.text("session_id")).session
    by_id = {p.online_id: p for p in session.sources}
    candidates = [_player(by_id, online_id) for online_id in c.r.strs("candidate_ids", default=list(by_id))]
    stop = session.probe_hosts(candidates, cancel=c.token)
    return {
        "known_sources": [dto.known_source(source) for source in session.known_sources.values()],
        "stopped": describe(stop) if stop is not None else None,
    }


def reload_episode(c: Call) -> Any:
    found = c.session(c.r.text("session_id"))
    found.session.reload(cancel=c.token)
    return {"sources": sources(found)}


def close_session(c: Call) -> Any:
    """Operations still running on the session are cancelled and can no longer report success."""
    session_id = c.r.text("session_id")
    with c.bridge.lock:
        session = c.entry.sessions.pop(session_id, None)
        if session is None:
            return {"closed": False, "cancelled_operations": 0}
        cancelled = c.bridge.close_session_locked(session, "close_session")
    return {"closed": True, "cancelled_operations": cancelled}


def history_watch(c: Call) -> Any:
    return {"entries": [dto.watch_entry(entry) for entry in c.engine.history.watch_entries()]}


def history_downloads(c: Call) -> Any:
    return {"entries": [dto.download_entry(entry) for entry in c.engine.history.download_entries()]}


def history_record_watch(c: Call) -> Any:
    c.engine.history.record_watch(common.series(c.r), c.r.number("episode"))
    return {}


def history_record_download(c: Call) -> Any:
    c.engine.history.record_download(common.series(c.r), c.r.number("episode"))
    return {}


def history_clear(c: Call) -> Any:
    c.engine.history.clear()
    return {}


METHODS = {
    "search": method(search, "query"),
    "metadata": method(metadata, "url"),
    "metadata_many": method(metadata_many, "urls"),
    "episodes": method(episodes, "series", "url"),
    "open_episode": method(open_episode, "episode"),
    "select": method(select, "session_id", "filter", "purpose"),
    "resolve": method(
        resolve, "session_id", "online_id", "candidate_ids", "policy", "rank_by_health", "force_fresh", "quality"
    ),
    "probe_hosts": method(probe_hosts, "session_id", "candidate_ids"),
    "reload_episode": method(reload_episode, "session_id"),
    "close_session": method(close_session, "session_id"),
    "history_watch": method(history_watch),
    "history_downloads": method(history_downloads),
    "history_record_watch": method(history_record_watch, "series", "episode"),
    "history_record_download": method(history_record_download, "series", "episode"),
    "history_clear": method(history_clear),
}
