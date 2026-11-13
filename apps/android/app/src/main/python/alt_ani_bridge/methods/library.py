"""Library operations, one result per item; watch history is never touched by them."""

from pathlib import Path
from typing import Any

from alt_ani_bridge import dto_state
from alt_ani_bridge.context import Call
from alt_ani_bridge.methods import method
from alt_ani_bridge.platform import EXPORT_TARGET
from alt_ani_core.services.offline.library import ItemResult, ResultKind
from alt_ani_core.state import ItemStatus


def batch(results: list[ItemResult]) -> dict[str, Any]:
    """Always a success envelope: ``interrupted`` and ``not_attempted`` say what a cancellation left untouched."""
    done = [r for r in results if r.kind is not ResultKind.NOT_ATTEMPTED]
    skipped = [r.item_id for r in results if r.kind is ResultKind.NOT_ATTEMPTED]
    out: dict[str, Any] = {
        "results": [
            {
                "item_id": r.item_id,
                "result": r.kind.value,
                "code": r.code,
                **({"file_name": r.file_name} if r.file_name else {}),
            }
            for r in done
        ]
    }
    if skipped or any(r.code == "cancelled" for r in done):
        out |= {"interrupted": "cancelled", "not_attempted": skipped}
    return out


def library_list(c: Call) -> Any:
    items = c.runtime.state.library.items(series_id=c.r.text("series_id", None), status=c.r.enum("status", ItemStatus, None))
    return {"items": [dto_state.item(i) for i in items]}


def library_item(c: Call) -> Any:
    item = c.runtime.state.library.get(c.r.text("item_id"))
    return {"item": dto_state.item(item), "watch": dto_state.watch(c.runtime.state.watch.get(item.key), item.key)}


def library_verify(c: Call) -> Any:
    ids = c.r.strs("item_ids", "item_ids", default=None)
    results = c.runtime.library().verify(ids, deep=c.r.flag("deep", False), cancel=c.token)
    c.runtime.changed()
    return batch(results)


def library_delete(c: Call) -> Any:
    results = c.runtime.library().delete(c.r.strs("item_ids", "item_ids"), cancel=c.token)
    c.runtime.changed()
    return batch(results)


def library_retry_deletes(c: Call) -> Any:
    results = c.runtime.library().retry_pending_deletes(cancel=c.token)
    c.runtime.changed()
    return batch(results)


def library_export(c: Call) -> Any:
    """Not idempotent: after a lost reply the client checks the destination before repeating."""
    ids = c.r.strs("item_ids", "item_ids")
    on_conflict = c.r.choice("on_conflict", ("fail", "rename"), "fail")
    t = c.r.obj("target", ("kind", "path", "handle"))
    kind = t.choice("kind", ("directory", "platform"))
    if kind == "directory":
        target: Any = Path(t.text("path"))
    else:
        handle = t.text("handle")
        target = c.bridge.platform.acquire(handle, EXPORT_TARGET, t.field("handle"))
        c.operation.platform_handles.append(handle)
    results = c.runtime.library().export(ids, target, on_conflict=on_conflict, cancel=c.token)
    return batch(results)


METHODS = {
    "library_list": method(library_list, "series_id", "status"),
    "library_item": method(library_item, "item_id"),
    "library_verify": method(library_verify, "item_ids", "deep"),
    "library_delete": method(library_delete, "item_ids"),
    "library_retry_deletes": method(library_retry_deletes),
    "library_export": method(library_export, "item_ids", "target", "on_conflict"),
}
