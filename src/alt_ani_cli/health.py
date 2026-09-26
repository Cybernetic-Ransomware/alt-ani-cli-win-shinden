"""Session-scoped resolver health model and failure classifier.

Pure domain logic — no network, no persistence, no singleton. A ``ResolverHealth`` instance
is created explicitly per session/run by its caller; this module only defines the model.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

EVIDENCE_TTL_SEC = 600.0


class HostState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class Signal(StrEnum):
    SUCCESS = "success"
    FILE = "file"
    SOFT = "soft"
    TRANSIENT = "transient"
    HARD = "hard"
    UNSUPPORTED = "unsupported"
    IGNORE = "ignore"


class _LayerEffect(IntEnum):
    """Internal severity of a single resolver layer's failure — never exposed outside classify_failure()."""

    FILE = 0
    SOFT = 1
    TRANSIENT = 2
    NETWORK = 3


@dataclass(frozen=True)
class HealthTransition:
    host: str
    before: HostState
    after: HostState
    signal: Signal
    online_id: str
    evidence_expired: bool


# Mirrors extract/common.py's CATEGORY_* constants; duplicated (not imported) so health.py stays extract-independent.
_CATEGORY_EFFECTS: dict[str, _LayerEffect] = {
    "no_stream_url": _LayerEffect.FILE,
    "parser_drift": _LayerEffect.SOFT,
    "unknown": _LayerEffect.SOFT,
    "unsupported_host": _LayerEffect.SOFT,
    "timeout": _LayerEffect.TRANSIENT,
    "network_error": _LayerEffect.NETWORK,
}


def _http_error_effect(http_status: int | None) -> _LayerEffect:
    if http_status is None:
        return _LayerEffect.SOFT
    if http_status in (404, 410):
        return _LayerEffect.FILE
    if http_status == 429:
        return _LayerEffect.TRANSIENT
    if 500 <= http_status <= 599:
        return _LayerEffect.TRANSIENT
    if 400 <= http_status <= 499:
        return _LayerEffect.SOFT
    return _LayerEffect.SOFT


def _layer_effect(category: str, http_status: int | None) -> _LayerEffect:
    if category == "http_error":
        return _http_error_effect(http_status)
    # Unrecognized categories default to SOFT — never assume a strong signal we weren't told about.
    return _CATEGORY_EFFECTS.get(category, _LayerEffect.SOFT)


def classify_failure(
    host: str | None,
    *,
    layer: str,
    category: str | None,
    http_status: int | None,
    used_fallback: bool,
    fallback_category: str | None,
    fallback_http_status: int | None,
) -> Signal:
    if host is None or host == "unknown-host":
        return Signal.IGNORE
    if layer == "shinden_api":
        return Signal.IGNORE
    if category is None:
        return Signal.IGNORE
    if layer == "unsupported":
        return Signal.UNSUPPORTED

    effects = [_layer_effect(category, http_status)]
    if used_fallback and fallback_category is not None:
        effects.append(_layer_effect(fallback_category, fallback_http_status))

    worst = min(effects)
    if worst is _LayerEffect.FILE:
        return Signal.FILE
    if worst is _LayerEffect.SOFT:
        return Signal.SOFT
    if worst is _LayerEffect.TRANSIENT:
        return Signal.TRANSIENT
    return Signal.HARD if len(effects) >= 2 else Signal.TRANSIENT


@dataclass
class _HostRecord:
    successes: int = 0
    soft_ids: set[str] = field(default_factory=set)
    transport_ids: set[str] = field(default_factory=set)
    hard: bool = False
    unsupported: bool = False
    last_failure_at: float | None = None
    last_state: HostState = HostState.UNKNOWN


class ResolverHealth:
    """Session-scoped resolver health store. Not a singleton — create one per session/run."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = EVIDENCE_TTL_SEC) -> None:
        self._clock = clock
        self._ttl = ttl
        self._hosts: dict[str, _HostRecord] = {}

    def _evidence_active(self, record: _HostRecord, now: float) -> bool:
        return record.last_failure_at is not None and (now - record.last_failure_at) <= self._ttl

    def _compute_state(self, record: _HostRecord, now: float) -> HostState:
        if record.unsupported:
            return HostState.UNAVAILABLE
        evidence_active = self._evidence_active(record, now)
        if evidence_active and (record.hard or len(record.transport_ids) >= 2):
            return HostState.UNAVAILABLE
        if evidence_active and (record.soft_ids or record.transport_ids):
            return HostState.DEGRADED
        if record.successes > 0:
            return HostState.HEALTHY
        return HostState.UNKNOWN

    def state(self, host: str) -> HostState:
        record = self._hosts.get(host)
        if record is None:
            return HostState.UNKNOWN
        return self._compute_state(record, self._clock())

    def should_defer(self, host: str) -> bool:
        record = self._hosts.get(host)
        if record is None or record.unsupported:
            return False
        return self._compute_state(record, self._clock()) == HostState.UNAVAILABLE

    def _apply(self, record: _HostRecord, signal: Signal, online_id: str, now: float) -> None:
        if signal is Signal.SUCCESS:
            record.soft_ids.clear()
            record.transport_ids.clear()
            record.hard = False
            record.last_failure_at = None
            record.successes += 1
        elif signal is Signal.FILE:
            record.transport_ids.clear()
            record.hard = False
        elif signal is Signal.SOFT:
            record.soft_ids.add(online_id)
            record.last_failure_at = now
        elif signal is Signal.TRANSIENT:
            record.transport_ids.add(online_id)
            record.last_failure_at = now
        elif signal is Signal.HARD:
            record.transport_ids.add(online_id)
            record.hard = True
            record.last_failure_at = now
        elif signal is Signal.UNSUPPORTED:
            record.unsupported = True

    def record(self, host: str | None, online_id: str, signal: Signal) -> HealthTransition | None:
        if host is None or signal is Signal.IGNORE:
            return None

        record = self._hosts.setdefault(host, _HostRecord())
        now = self._clock()

        evidence_expired = False
        if record.last_failure_at is not None and (now - record.last_failure_at) > self._ttl:
            record.soft_ids.clear()
            record.transport_ids.clear()
            record.hard = False
            record.last_failure_at = None
            evidence_expired = True

        self._apply(record, signal, online_id, now)

        before = record.last_state
        after = self._compute_state(record, now)
        record.last_state = after

        if after != before or evidence_expired:
            return HealthTransition(
                host=host,
                before=before,
                after=after,
                signal=signal,
                online_id=online_id,
                evidence_expired=evidence_expired,
            )
        return None
