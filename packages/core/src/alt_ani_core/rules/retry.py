"""How an attempt ends: which store operation records it and whether it spends one of the bounded retries."""

from dataclasses import dataclass
from enum import StrEnum

from alt_ani_core.state import JobError, WaitReason

MAX_RETRIES = 3
BACKOFF_SEC = (30, 120, 600)


class Action(StrEnum):
    WAIT = "wait"
    DEFER = "defer"
    FAIL = "fail"


@dataclass(frozen=True)
class Outcome:
    action: Action
    error: JobError
    reason: WaitReason | None = None
    delay_sec: float = 0.0
    counts: bool = True


def _params(**values: str | int | float | bool | None) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    return tuple(sorted((k, v) for k, v in values.items() if v is not None))


def wait(reason: WaitReason, code: str, /, **params: str | int | float | bool | None) -> Outcome:
    return Outcome(Action.WAIT, JobError(code, _params(**params)), reason=reason)


def fail(code: str, /, **params: str | int | float | bool | None) -> Outcome:
    return Outcome(Action.FAIL, JobError(code, _params(**params)))


def transient(retry_count: int, code: str, /, *, retry_after: float | None = None, **params) -> Outcome:
    """Deferred with backoff while retries remain; afterwards a failure the user retries by hand."""
    if retry_count >= MAX_RETRIES:
        return fail(code, retries=retry_count, **params)
    delay = max(BACKOFF_SEC[min(retry_count, len(BACKOFF_SEC) - 1)], retry_after or 0.0)
    return Outcome(Action.DEFER, JobError(code, _params(**params)), delay_sec=delay)


_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


def transfer_outcome(category: str, http_status: int | None, *, network_allowed: bool, retry_count: int) -> Outcome:
    """Categories come from the transfer's classification; messages (which may carry URLs) are never consulted."""
    if category == "storage.no_space":
        return wait(WaitReason.STORAGE, "storage.no_space")
    if category in ("storage.access_denied", "storage.outside_root"):
        return fail(category)
    if not network_allowed and category in ("timeout", "network_error", "http_error", "unknown"):
        return Outcome(Action.WAIT, JobError("network.lost"), reason=WaitReason.NETWORK, counts=False)
    if category in ("timeout", "network_error", "storage.in_use"):
        return transient(retry_count, f"transfer.{category.removeprefix('storage.')}")
    if category == "http_error":
        if http_status in _TRANSIENT_HTTP or http_status is None:
            return transient(retry_count, "transfer.http_error", http_status=http_status)
        return transient(retry_count, "transfer.source_refused", http_status=http_status)
    return fail("transfer.failed", category=category)
