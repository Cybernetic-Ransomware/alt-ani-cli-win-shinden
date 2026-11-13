"""Preferences, the network the platform reports, and per-handle access."""

import dataclasses
from typing import Any

from alt_ani_bridge import dto_state
from alt_ani_bridge.context import Call, EngineEntry
from alt_ani_bridge.methods import common, method
from alt_ani_bridge.schema import InputError
from alt_ani_core.config import AccessConfig
from alt_ani_core.rules.conditions import flaresolverr_allowed
from alt_ani_core.state import DownloadNetwork, NetworkKind

_PREFERENCE_FIELDS = ("audio", "subs", "quality", "download_network", "auto_alternatives", "max_active_downloads")


def effective_access(access: AccessConfig, network: NetworkKind) -> AccessConfig:
    """The solver only on Wi-Fi: the same rule the executor applies to its own engine."""
    return dataclasses.replace(access, use_flaresolverr=flaresolverr_allowed(access.use_flaresolverr, network))


def apply_access(c: Call, entry: EngineEntry, access: AccessConfig, reason: str) -> int:
    """A new effective access makes every session of the handle stale; they are closed with ``reason``."""
    entry.access = access
    entry.engine.set_access(effective_access(access, entry.runtime.network.current()))
    return c.drop_sessions(reason)


def set_access(c: Call) -> Any:
    access = common.access(c.r) or AccessConfig()
    c.commit()
    return {"dropped_sessions": apply_access(c, c.entry, access, "set_access")}


def engine_status(c: Call) -> Any:
    entry = c.entry
    with c.bridge.lock:
        return {
            "sessions": len(entry.sessions),
            "running_operations": len(entry.operations - {c.operation.key}),
            "listener_errors": entry.push.errors if entry.push else 0,
            "engine_started": entry.transport.started,
            "events_dropped": entry.runtime.journal.dropped,
        }


def preferences_get(c: Call) -> Any:
    return dto_state.preferences(c.runtime.state.preferences.get())


def preferences_update(c: Call) -> Any:
    ch = c.r.obj("changes", _PREFERENCE_FIELDS)
    expected = c.r.integer("expected_revision", minimum=0)
    changes: dict[str, Any] = {}
    for name in ("audio", "subs"):
        if ch.has(name):
            changes[name] = ch.text(name)
    if ch.has("quality"):
        changes["quality"] = common.quality(ch.text("quality"), ch.field("quality"))
    if ch.has("download_network"):
        changes["download_network"] = ch.enum("download_network", DownloadNetwork)
    if ch.has("auto_alternatives"):
        changes["auto_alternatives"] = ch.flag("auto_alternatives")
    if ch.has("max_active_downloads"):
        changes["max_active_downloads"] = ch.integer("max_active_downloads", minimum=1, maximum=16)
    c.commit()
    try:
        prefs = c.runtime.state.preferences.update(expected_revision=expected, **changes)
    except ValueError:
        raise InputError("changes", "invalid_preferences") from None
    c.runtime.changed()
    return dto_state.preferences(prefs)


def network_set(c: Call) -> Any:
    """One network for the runtime: the executor and the watching gate both follow it."""
    runtime = c.runtime
    kind = c.r.enum("kind", NetworkKind)
    c.commit()
    before = runtime.network.current()
    runtime.network.set(kind)
    closed = 0
    if kind is not before:
        with c.bridge.lock:
            entries = [e for e in c.bridge.engine_entries() if e.runtime is runtime]
        for entry in entries:
            if effective_access(entry.access, kind) != effective_access(entry.access, before):
                entry.engine.set_access(effective_access(entry.access, kind))
                closed += Call(c.bridge, c.r, c.operation, entry).drop_sessions("network_changed")
        c.publish("network.changed", {"kind": kind.value})
        if kind is NetworkKind.CELLULAR:
            for session_id, key in runtime.sessions_without_consent():
                c.publish(
                    "playback.consent_required",
                    {"playback_session": {"id": session_id, "series_id": key.series_id, "number": key.number}},
                )
        runtime.changed()
    return {"kind": kind.value, "sessions_closed": closed}


def network_get(c: Call) -> Any:
    return {"kind": c.runtime.network.current().value}


def storage_changed(c: Call) -> Any:
    queue = c.runtime.queue
    if queue is not None:
        queue.notify_storage_changed()
    return {"executor_notified": queue is not None and queue.running}


METHODS = {
    "set_access": method(set_access, "access"),
    "engine_status": method(engine_status),
    "preferences_get": method(preferences_get),
    "preferences_update": method(preferences_update, "changes", "expected_revision"),
    "network_set": method(network_set, "kind"),
    "network_get": method(network_get),
    "storage_changed": method(storage_changed),
}
