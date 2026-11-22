"""Host objects (a Kotlin file store or export target) registered once and referred to by an opaque handle."""

import threading
import uuid
from dataclasses import dataclass
from typing import Any

from alt_ani_bridge.errors import BridgeError
from alt_ani_bridge.schema import InputError

FILE_STORE = "file_store"
EXPORT_TARGET = "export_target"

_FILE_STORE_MEMBERS = (
    "backend",
    "check_root",
    "partial_locator",
    "work_path",
    "allocate",
    "publish",
    "stat",
    "digest",
    "remove",
    "open_read",
    "free_space",
    "scan",
    "playback_target",
)


def kind_of(obj: Any) -> str | None:
    if all(hasattr(obj, name) for name in _FILE_STORE_MEMBERS):
        return FILE_STORE
    if callable(getattr(obj, "create", None)):
        return EXPORT_TARGET
    return None


@dataclass
class _Entry:
    obj: Any
    kind: str
    uses: int = 0


class PlatformObjects:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def register(self, obj: Any) -> tuple[str, str]:
        kind = kind_of(obj)
        if kind is None:
            raise InputError("object", "unsupported_platform_object", expected=[FILE_STORE, EXPORT_TARGET])
        handle = uuid.uuid4().hex
        with self._lock:
            self._entries[handle] = _Entry(obj, kind)
        return handle, kind

    def acquire(self, handle: str, kind: str, field: str) -> Any:
        """Counted until ``release_use``; an object in use cannot be released by the host."""
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None:
                raise BridgeError("unknown_platform_object", "no such platform object", handle=handle)
            if entry.kind != kind:
                raise InputError(field, "wrong_platform_object", expected=kind)
            entry.uses += 1
            return entry.obj

    def release_use(self, handle: str) -> None:
        with self._lock:
            entry = self._entries.get(handle)
            if entry is not None and entry.uses > 0:
                entry.uses -= 1

    def release(self, handle: str) -> None:
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None:
                raise BridgeError("unknown_platform_object", "no such platform object", handle=handle)
            if entry.uses > 0:
                raise BridgeError("platform.in_use", "the platform object is still in use", handle=handle, uses=entry.uses)
            del self._entries[handle]
