"""Pull access to the runtime's event ring; a waiting ``events_next`` is cancelled through its ``op_id``."""

from typing import Any

from alt_ani_bridge.context import Call
from alt_ani_bridge.methods import method


def events_head(c: Call) -> Any:
    """Takes the watcher's base now, so any change after this head is signalled with a later ``seq``."""
    runtime = c.runtime
    runtime.watcher.sync()
    return {"epoch": runtime.journal.epoch, "seq": runtime.journal.head()}


def events_next(c: Call) -> Any:
    runtime = c.runtime
    cursor = c.r.obj("cursor", ("epoch", "seq"))
    epoch, seq = cursor.text("epoch"), cursor.integer("seq", minimum=1)
    limit = c.r.integer("max", 100, minimum=1, maximum=500)
    wait_ms = c.r.integer("wait_ms", 0, minimum=0, maximum=30000)
    journal = runtime.journal
    if epoch != journal.epoch:
        return {"epoch": journal.epoch, "events": [], "next_seq": journal.head(), "resync": True}
    runtime.watcher.attach()
    try:
        events, next_seq, resync = journal.read(seq, limit, wait_ms / 1000, c.token)
    finally:
        runtime.watcher.detach()
    return {"epoch": journal.epoch, "events": events, "next_seq": next_seq, "resync": resync}


def events_status(c: Call) -> Any:
    runtime = c.runtime
    with c.bridge.lock:
        entries = [e for e in c.bridge.engine_entries() if e.runtime is runtime]
    return {
        "epoch": runtime.journal.epoch,
        "oldest_seq": runtime.journal.oldest(),
        "head_seq": runtime.journal.head(),
        "listener_errors": sum(e.push.errors for e in entries if e.push),
        "dropped": runtime.journal.dropped,
    }


METHODS = {
    "events_head": method(events_head),
    "events_next": method(events_next, "cursor", "max", "wait_ms"),
    "events_status": method(events_status),
}
