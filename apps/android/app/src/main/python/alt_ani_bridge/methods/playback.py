"""Preparing and refreshing playback; the rules live in the engine's playback service, the bridge keeps the choice."""

from typing import Any

from alt_ani_bridge import dto, dto_state
from alt_ani_bridge.context import Call, SessionEntry
from alt_ani_bridge.methods import common, method
from alt_ani_bridge.schema import InputError
from alt_ani_core.rules.criteria import criteria_for, item_matches, variant_identity
from alt_ani_core.rules.episodes import later_available, next_episode
from alt_ani_core.services.playback import OnlinePlayback, check_stream
from alt_ani_core.state import EpisodeKey, ItemStatus, PlaybackSession


def _online(found: SessionEntry, result: OnlinePlayback) -> dict[str, Any]:
    variant = dto.variant(result.variant)
    return {
        "mode": "online",
        "session_id": found.session_id,
        "source": dto.player(result.source),
        "matches_criteria": result.matches_criteria,
        "criteria": dto_state.criteria(result.selection.criteria),
        "variant": {**variant, "identity": dto_state.variant_id(result.selection.variant)},
        "variants": [dto_state.variant_id(variant_identity(v)) for v in result.stream.variants],
        "variant_changed": result.variant_changed,
    }


def _gate(c: Call, session: PlaybackSession) -> None:
    check_stream(c.runtime.network.current(), c.runtime.consent(session.session_id), session.key)


def playback_prepare(c: Call) -> Any:
    runtime = c.runtime
    session = common.playback_session(c.r)
    series, episode = common.series(c.r), common.episode(c.r)
    key = EpisodeKey(series.id, episode.number)
    if key != session.key:
        raise InputError("playback_session", "other_episode")
    criteria = common.criteria_overrides(c.r, runtime.state.preferences.get())
    resume = dto_state.resume(runtime.state.watch.get(key))
    chosen, others = runtime.playback.local_copies(key, criteria)
    other_copies = [dto_state.item(i) for i in others]
    if c.r.flag("prefer_local", True) and chosen is not None and runtime.store is not None:
        target = runtime.store.playback_target(chosen.spec.locator)
        return {
            "mode": "local",
            "item_id": chosen.id,
            "target": dto_state.target(target),
            "item": dto_state.item(chosen),
            "other_local_copies": other_copies,
            "resume": resume,
        }
    _gate(c, session)
    if c.r.has("session_id"):
        found = c.session(c.r.text("session_id"))
        if found.episode.number != episode.number:
            raise InputError("session_id", "other_episode")
    else:
        found = c.open_session(episode)
    online_id = c.r.text("online_id", None)
    result = runtime.playback.online(found.session, criteria, online_id=online_id, cancel=c.token)
    with c.bridge.lock:
        found.selection = result.selection
        found.playback_session_id = session.session_id
    return {**_online(found, result), "other_local_copies": other_copies, "resume": resume}


def stream_refresh(c: Call) -> Any:
    """Keeps the chosen source, variant and criteria; checks the network and consent again before any request."""
    session = common.playback_session(c.r)
    found = c.session(c.r.text("session_id"))
    selection = found.selection
    if selection is None or found.playback_session_id != session.session_id:
        raise InputError("session_id", "no_playback_selection")
    _gate(c, session)
    variant = common.variant(c.r)
    result = c.runtime.playback.refresh(found.session, selection, variant=variant, cancel=c.token)
    with c.bridge.lock:
        found.selection = result.selection
    return _online(found, result)


def episode_next(c: Call) -> Any:
    """P4: the next episode of the known order; a later local copy is offered separately, never as the next one."""
    runtime = c.runtime
    series_id = c.r.text("series_id")
    current = c.r.number("current")
    criteria = criteria_for(runtime.state.preferences.get())
    copies = {
        i.key.number: i
        for i in runtime.state.library.items(series_id=series_id, status=ItemStatus.AVAILABLE)
        if item_matches(i.spec, criteria)
    }
    nxt = next_episode(c.r.numbers("episodes"), current) if c.r.has("episodes") else None
    result: dict[str, Any] = {"basis": "listing" if c.r.has("episodes") else "none", "next": None, "later_local_copy": None}
    if nxt is not None:
        copy = copies.get(nxt)
        result["next"] = {"number": nxt, "local_copy": dto_state.item(copy) if copy else None}
    if nxt is None or nxt not in copies:
        later = later_available(copies, current, exclude=nxt)
        if later is not None:
            result["later_local_copy"] = {"number": later, "item": dto_state.item(copies[later])}
    return result


METHODS = {
    "playback_prepare": method(
        playback_prepare, "playback_session", "series", "episode", "criteria", "prefer_local", "session_id", "online_id"
    ),
    "stream_refresh": method(stream_refresh, "session_id", "playback_session", "variant"),
    "episode_next": method(episode_next, "series_id", "current", "episodes"),
}
