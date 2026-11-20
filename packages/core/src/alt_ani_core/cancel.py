"""Cooperative cancellation shared by every blocking wait in the engine."""

import threading

from alt_ani_core.errors import OperationCancelled


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled()

    def wait(self, seconds: float) -> None:
        """Sleep that ends at once when the token is cancelled."""
        if self._event.wait(max(seconds, 0.0)):
            raise OperationCancelled()


class _NeverCancelled(CancelToken):
    def cancel(self) -> None:
        raise RuntimeError("NEVER is shared by every caller that passed no token and must not be cancelled")


NEVER: CancelToken = _NeverCancelled()
