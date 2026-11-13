"""Watch state: opening, position fenced by the playback session's ``seq``, the watched flag with a revision."""

from typing import Any

from alt_ani_bridge import dto_state
from alt_ani_bridge.context import Call
from alt_ani_bridge.errors import BridgeError
from alt_ani_bridge.methods import common, method
from alt_ani_core.state import EpisodeKey, OpenedVia


def playback_begin(c: Call) -> Any:
    series, episode = common.series(c.r), common.episode(c.r)
    session, state = c.runtime.state.watch.start_playback(series, episode)
    c.runtime.begin_playback(session)
    c.runtime.changed()
    return {"playback_session": dto_state.playback_session(session), "watch": dto_state.watch(state, session.key)}


def playback_position(c: Call) -> Any:
    session = common.playback_session(c.r)
    accepted = c.runtime.state.watch.record_position(
        session,
        c.r.integer("seq", minimum=1),
        c.r.integer("position_ms", minimum=0),
        c.r.integer("duration_ms", None, minimum=1, nullable=True),
    )
    return {"accepted": accepted}


def playback_ended(c: Call) -> Any:
    """The only automatic way to mark an episode watched; ends the viewing and its mobile-data consent."""
    session = common.playback_session(c.r)
    watch = c.runtime.state.watch
    accepted = watch.record_playback_ended(
        session, c.r.integer("seq", minimum=1), c.r.integer("position_ms", minimum=0), c.r.integer("duration_ms", minimum=1)
    )
    c.runtime.end_playback(session.session_id)
    c.runtime.changed()
    return {"accepted": accepted, "watch": dto_state.watch(watch.get(session.key), session.key)}


def playback_close(c: Call) -> Any:
    session = common.playback_session(c.r)
    c.runtime.end_playback(session.session_id)
    return {"closed": True}


def playback_allow_cellular(c: Call) -> Any:
    session = common.playback_session(c.r)
    if not c.runtime.allow_cellular(session.session_id, session.key):
        raise BridgeError(
            "playback.not_open", "this playback session is not the open viewing", playback_session=session.session_id
        )
    return {"allowed": True}


def watch_set_watched(c: Call) -> Any:
    key = common.key(c.r)
    state = c.runtime.state.watch.set_watched(key, c.r.flag("watched"), c.r.integer("expected_revision", minimum=0))
    c.runtime.changed()
    return {"watch": dto_state.watch(state, key)}


def watch_get(c: Call) -> Any:
    series_id = c.r.text("series_id")
    watch = c.runtime.state.watch
    if c.r.has("numbers"):
        keys = [EpisodeKey(series_id, n) for n in c.r.numbers("numbers")]
        return {"episodes": [dto_state.watch(watch.get(k), k) for k in keys]}
    return {"episodes": [dto_state.watch(s, s.key) for s in watch.for_series(series_id)]}


def watch_series(c: Call) -> Any:
    series_id = c.r.text("series_id")
    watch = c.runtime.state.watch
    last = watch.last_opened(series_id)
    return {
        "last_opened": dto_state.watch(last, last.key) if last else None,
        "episodes": [dto_state.watch(s, s.key) for s in watch.for_series(series_id)],
    }


def watch_recent(c: Call) -> Any:
    rows = c.runtime.state.watch.recent_series(c.r.integer("limit", 50, minimum=1, maximum=500))
    return {
        "series": [
            {"series": {"id": ref.id, "slug": ref.slug, "title": ref.title, "url": ref.url}, "watch": dto_state.watch(s, s.key)}
            for ref, s in rows
        ]
    }


def watch_open_in_browser(c: Call) -> Any:
    """Records the opening only: the known position and the watched flag stay as they are."""
    series, episode = common.series(c.r), common.episode(c.r)
    url, target = episode.url, "episode_page"
    if c.r.has("session_id"):
        session = c.session(c.r.text("session_id")).session
        known = session.known_sources.get(c.r.text("online_id")) if c.r.has("online_id") else None
        if known is not None:
            url, target = known.embed_url, "source"
    state = c.runtime.state.watch.record_opened(series, episode, OpenedVia.BROWSER)
    c.runtime.changed()
    return {"target": target, "url": url, "watch": dto_state.watch(state, state.key)}


METHODS = {
    "playback_begin": method(playback_begin, "series", "episode"),
    "playback_position": method(playback_position, "playback_session", "seq", "position_ms", "duration_ms"),
    "playback_ended": method(playback_ended, "playback_session", "seq", "position_ms", "duration_ms"),
    "playback_close": method(playback_close, "playback_session"),
    "playback_allow_cellular": method(playback_allow_cellular, "playback_session"),
    "watch_set_watched": method(watch_set_watched, "series_id", "number", "watched", "expected_revision"),
    "watch_get": method(watch_get, "series_id", "numbers"),
    "watch_series": method(watch_series, "series_id"),
    "watch_recent": method(watch_recent, "limit"),
    "watch_open_in_browser": method(watch_open_in_browser, "series", "episode", "session_id", "online_id"),
}
