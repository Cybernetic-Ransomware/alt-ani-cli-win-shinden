"""Maps every failure to a stable, token-free error object; no Python exception crosses the bridge.

``code`` is stable per condition; ``kind`` is a closed set a client can switch on. Mapping goes by exception class.
"""

import re
from typing import Any

from alt_ani_core.errors import (
    AccessBlockedError,
    CriteriaMismatchError,
    EngineClosedError,
    ExecutorBusyError,
    ExecutorNotOwnerError,
    FreshResolveLimitError,
    HistoryBackupError,
    HistoryVersionError,
    InteractionRequiredError,
    InvalidTransitionError,
    OperationCancelled,
    PlaybackBlockedError,
    ReloadLimitError,
    ShindenError,
    ShindenXhrError,
    SourcesExhaustedError,
    StaleSessionError,
    StateConflictError,
    StorageError,
    UnknownRecordError,
)
from alt_ani_core.history.database import STORE_ERRORS
from alt_ani_core.redaction import redact_text
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.transport.api import HttpStatusError, TransportError

INVALID_INPUT = "invalid_input"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
NO_MATCH = "no_match"
WAITING = "waiting"
CANCELLED = "cancelled"
CLOSED = "closed"
UNAVAILABLE = "unavailable"
ACCESS = "access"
STORAGE = "storage"
LIMIT = "limit"
INTERNAL = "internal"

KINDS = (
    INVALID_INPUT,
    NOT_FOUND,
    CONFLICT,
    NO_MATCH,
    WAITING,
    CANCELLED,
    CLOSED,
    UNAVAILABLE,
    ACCESS,
    STORAGE,
    LIMIT,
    INTERNAL,
)

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")

_BRIDGE_KINDS = {
    "bad_request": INVALID_INPUT,
    "unsupported_contract": INVALID_INPUT,
    "unknown_method": INVALID_INPUT,
    "unknown_engine": NOT_FOUND,
    "unknown_session": NOT_FOUND,
    "unknown_source": NOT_FOUND,
    "unknown_platform_object": NOT_FOUND,
    "operation_id_in_use": CONFLICT,
    "runtime.config_conflict": CONFLICT,
    "runtime.closing": CONFLICT,
    "platform.in_use": CONFLICT,
    "playback.not_open": CONFLICT,
    "executor.closing": CONFLICT,
    "session_closed": CLOSED,
    "transport_unavailable": UNAVAILABLE,
    "storage.not_configured": STORAGE,
}

_WAITING_CODES = {"network.cellular_consent_required"}


class BridgeError(Exception):
    """A request the bridge itself rejects: unknown ids, malformed payloads, missing platform pieces."""

    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


def _error(
    code: str, kind: str, message: str, *, retryable: bool = False, params: dict[str, Any] | None = None, **more: Any
) -> dict[str, Any]:
    params = {**(params or {}), **more}
    return {"code": code, "kind": kind, "retryable": retryable, "message": message, "params": params}


def _xhr(exc: ShindenXhrError) -> dict[str, Any]:
    outcome = shinden_api.xhr_outcome(exc)
    return _error(
        f"xhr_{outcome.value}",
        UNAVAILABLE,
        shinden_api.stop_message(exc),
        retryable=outcome is shinden_api.XhrOutcome.SERVICE_UNAVAILABLE,
        stage=exc.stage,
        api_code=exc.api_code,
        http_status=exc.http_status,
        retry_after=exc.retry_after,
        reason=exc.reason,
    )


def _engine_kind(exc: ShindenError) -> str:
    if isinstance(exc, UnknownRecordError):
        return NOT_FOUND
    if isinstance(exc, StateConflictError | InvalidTransitionError | ExecutorBusyError | ExecutorNotOwnerError):
        return CONFLICT
    if isinstance(exc, CriteriaMismatchError):
        return NO_MATCH
    if isinstance(exc, InteractionRequiredError):
        return WAITING
    if isinstance(exc, PlaybackBlockedError):
        return WAITING if exc.code in _WAITING_CODES else UNAVAILABLE
    if isinstance(exc, StorageError):
        return STORAGE
    if isinstance(exc, ReloadLimitError | FreshResolveLimitError):
        return LIMIT
    if isinstance(exc, HistoryVersionError | HistoryBackupError):
        return INTERNAL
    if isinstance(exc, EngineClosedError):
        return CLOSED
    return UNAVAILABLE


def _scalars(params: Any) -> dict[str, Any]:
    return {key: value for key, value in dict(params).items() if isinstance(value, str | int | float | bool | None)}


def describe(exc: BaseException) -> dict[str, Any]:  # noqa: C901
    if isinstance(exc, OperationCancelled):
        return _error("cancelled", CANCELLED, "operation cancelled")
    if isinstance(exc, StaleSessionError):
        return _error("stale_session", CLOSED, "the engine's access changed or it was closed", retryable=True)
    if isinstance(exc, BridgeError):
        return _error(exc.code, _BRIDGE_KINDS.get(exc.code, INTERNAL), str(exc), params=exc.params)
    if isinstance(exc, EngineClosedError):
        return _error("engine_closed", CLOSED, str(exc))
    if isinstance(exc, ShindenXhrError):
        return _xhr(exc)
    if isinstance(exc, AccessBlockedError):
        return _error("access_blocked", ACCESS, redact_text(exc), block=exc.kind)
    if isinstance(exc, SourcesExhaustedError):
        attempts = [{"online_id": a.source.online_id, "host": a.host, "error": a.error} for a in exc.attempts]
        return _error("sources_exhausted", UNAVAILABLE, redact_text(exc), attempts=attempts)
    if isinstance(exc, HttpStatusError):
        if exc.status == 403:
            return _error("access_blocked", ACCESS, "HTTP 403", block="http_403")
        return _error("http_status", UNAVAILABLE, f"HTTP {exc.status}", retryable=exc.status >= 500, status=exc.status)
    if isinstance(exc, TransportError):
        return _error(exc.kind, UNAVAILABLE, exc.kind, retryable=True)
    if isinstance(exc, ShindenError):
        code = exc.code or _CAMEL.sub("_", type(exc).__name__.removesuffix("Error")).lower()
        kind = _engine_kind(exc)
        retryable = kind == STORAGE and code == "storage.unavailable"
        return _error(code, kind, redact_text(exc), retryable=retryable, params=_scalars(exc.params))
    if isinstance(exc, STORE_ERRORS):
        # the database file could not be read or written now (locked, full disk); its text may hold a path
        return _error("history.unavailable", STORAGE, type(exc).__name__, retryable=True)
    # an unexpected exception's text may hold a URL or token, so only its type is reported
    return _error("internal", INTERNAL, type(exc).__name__)
