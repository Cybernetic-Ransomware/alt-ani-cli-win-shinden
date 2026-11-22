"""The network the platform reports; the engine never detects it itself."""

import threading
from collections.abc import Callable
from typing import Protocol

from alt_ani_core.state import NetworkKind


class NetworkSource(Protocol):
    def current(self) -> NetworkKind: ...

    def subscribe(self, callback: Callable[[NetworkKind], None]) -> None: ...


class StaticNetwork:
    """Set by the application (desktop: Wi-Fi unless told otherwise) or by a test; notifies subscribers on change."""

    def __init__(self, kind: NetworkKind = NetworkKind.WIFI) -> None:
        self._kind = kind
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[NetworkKind], None]] = []

    def current(self) -> NetworkKind:
        with self._lock:
            return self._kind

    def subscribe(self, callback: Callable[[NetworkKind], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def set(self, kind: NetworkKind) -> None:
        with self._lock:
            if kind is self._kind:
                return
            self._kind = kind
            subscribers = list(self._subscribers)
        for callback in subscribers:
            callback(kind)
