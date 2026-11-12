"""Interaction with a host or Shinden: the job waits with its choice kept; grants live in memory only."""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

SCOPE_SHINDEN = "shinden"
SCOPE_HOST = "host"
GRANT_TTL_SEC = 30 * 60


class InteractionNeeded(Exception):  # noqa: N818 - a condition, not a failure
    """Raised by a source provider; ``host`` and ``online_id`` are names, never URLs."""

    def __init__(self, scope: str, host: str | None = None, online_id: str | None = None) -> None:
        super().__init__(scope)
        self.scope = scope
        self.host = host
        self.online_id = online_id


@dataclass(frozen=True)
class InteractionGrant:
    scope: str
    host: str | None = None
    values: Mapping[str, str] = field(default_factory=dict, repr=False)


class Grants:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[str, tuple[InteractionGrant, float]] = {}

    def put(self, job_id: str, grant: InteractionGrant) -> None:
        with self._lock:
            self._grants[job_id] = (grant, self._clock() + GRANT_TTL_SEC)

    def get(self, job_id: str) -> InteractionGrant | None:
        with self._lock:
            entry = self._grants.get(job_id)
            if entry is None:
                return None
            if entry[1] < self._clock():
                del self._grants[job_id]
                return None
            return entry[0]

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._grants.pop(job_id, None)
