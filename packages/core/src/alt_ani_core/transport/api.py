"""HTTP contracts the engine depends on; adapters translate their own exceptions into these."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

from alt_ani_core.config import HTTP_TIMEOUT_SEC


class TransportError(Exception):
    """Message and repr never carry the URL — request URLs hold tokens; ``url`` is for redacted display only."""

    kind = "network_error"

    def __init__(self, message: str = "", *, url: str | None = None) -> None:
        super().__init__(message or self.kind)
        self.url = url


class TransportTimeout(TransportError):
    kind = "timeout"


class NetworkError(TransportError):
    kind = "network_error"


class DecodeError(TransportError):
    """The body was not the JSON the caller asked for."""

    kind = "decode_error"


class HttpStatusError(TransportError):
    kind = "http_error"

    def __init__(self, status: int, *, url: str | None = None) -> None:
        super().__init__(f"HTTP {status}", url=url)
        self.status = status


class HttpResponse(Protocol):
    status_code: int
    text: str
    headers: Mapping[str, str]
    url: str

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


class HttpSession(Protocol):
    """One cookie jar; never shared between the Shinden session and extractor sessions."""

    def get(
        self, url: str, *, headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None
    ) -> HttpResponse: ...

    def post(
        self, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None, data: Any = None
    ) -> HttpResponse: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc: object) -> None: ...


@dataclass(frozen=True)
class SessionProfile:
    name: str
    headers: Mapping[str, str] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout: float = HTTP_TIMEOUT_SEC


EXTRACTOR_PROFILE = SessionProfile(name="extractor")


class Transport(Protocol):
    def open(self, profile: SessionProfile) -> HttpSession:
        """A new session with its own cookie jar on every call."""
        ...
