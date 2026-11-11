"""Maps every failure to a stable, token-free error object; no Python exception crosses the bridge."""

import re
from typing import Any

from alt_ani_core.errors import (
    AccessBlockedError,
    EngineClosedError,
    OperationCancelled,
    ShindenError,
    ShindenXhrError,
    SourcesExhaustedError,
    StaleSessionError,
)
from alt_ani_core.redaction import redact_text
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.transport.api import HttpStatusError, TransportError

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


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
        "shinden_api",
        shinden_api.stop_message(exc),
        retryable=outcome is shinden_api.XhrOutcome.SERVICE_UNAVAILABLE,
        stage=exc.stage,
        api_code=exc.api_code,
        http_status=exc.http_status,
        retry_after=exc.retry_after,
        reason=exc.reason,
    )


def describe(exc: BaseException) -> dict[str, Any]:  # noqa: C901
    if isinstance(exc, OperationCancelled):
        return _error("cancelled", "cancelled", "operation cancelled")
    if isinstance(exc, StaleSessionError):
        return _error("stale_session", "stale", "the engine's access changed or it was closed", retryable=True)
    if isinstance(exc, BridgeError):
        return _error(exc.code, "bridge", str(exc), params=exc.params)
    if isinstance(exc, EngineClosedError):
        return _error("engine_closed", "bridge", str(exc))
    if isinstance(exc, ShindenXhrError):
        return _xhr(exc)
    if isinstance(exc, AccessBlockedError):
        return _error("access_blocked", "access", redact_text(exc), block=exc.kind)
    if isinstance(exc, SourcesExhaustedError):
        attempts = [{"online_id": a.source.online_id, "host": a.host, "error": a.error} for a in exc.attempts]
        return _error("sources_exhausted", "sources", redact_text(exc), attempts=attempts)
    if isinstance(exc, HttpStatusError):
        if exc.status == 403:
            return _error("access_blocked", "access", "HTTP 403", block="http_403")
        return _error("http_status", "network", f"HTTP {exc.status}", retryable=exc.status >= 500, status=exc.status)
    if isinstance(exc, TransportError):
        return _error(exc.kind, "network", exc.kind, retryable=True)
    if isinstance(exc, ShindenError):
        code = exc.code or _CAMEL.sub("_", type(exc).__name__.removesuffix("Error")).lower()
        params = {key: value for key, value in exc.params.items() if isinstance(value, str | int | float | bool | None)}
        return _error(code, "engine", redact_text(exc), params=params)
    if isinstance(exc, KeyError | TypeError | ValueError):
        return _error("bad_request", "bridge", f"malformed request: {type(exc).__name__}")
    # an unexpected exception's text may hold a URL or token, so only its type is reported
    return _error("internal", "internal", type(exc).__name__)
