"""curl_cffi adapter: the only module that imports curl_cffi, kept for its browser TLS impersonation."""

from collections.abc import Mapping
from typing import Any, Self

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import exceptions as cffi_exceptions

from alt_ani_core.transport.api import (
    DecodeError,
    HttpStatusError,
    NetworkError,
    SessionProfile,
    TransportError,
    TransportTimeout,
)


def _translate(exc: Exception, url: str) -> TransportError:
    if isinstance(exc, cffi_exceptions.Timeout):
        return TransportTimeout(url=url)
    if isinstance(exc, cffi_exceptions.JSONDecodeError):
        return DecodeError(url=url)
    if isinstance(exc, cffi_exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return HttpStatusError(status if isinstance(status, int) else 0, url=url)
    return NetworkError(url=url)


class CurlResponse:
    def __init__(self, raw: Any, url: str) -> None:
        self._raw = raw
        self.url = url
        self.status_code: int = raw.status_code
        self.headers: Mapping[str, str] = raw.headers

    @property
    def text(self) -> str:
        return self._raw.text

    def json(self) -> Any:
        try:
            return self._raw.json()
        except ValueError:
            raise DecodeError(url=self.url) from None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpStatusError(self.status_code, url=self.url)


class CurlSession:
    def __init__(self, profile: SessionProfile) -> None:
        self._session = cffi_requests.Session(
            impersonate="chrome",
            headers=dict(profile.headers),
            cookies=dict(profile.cookies),
            timeout=profile.timeout,
            allow_redirects=True,
        )

    def _request(self, send: Any, url: str, **kwargs: Any) -> CurlResponse:
        try:
            raw = send(url, **{k: v for k, v in kwargs.items() if v is not None})
        except cffi_exceptions.RequestException as exc:
            # the curl message embeds the request URL, so the original exception is not chained
            raise _translate(exc, url) from None
        return CurlResponse(raw, url)

    def get(self, url: str, *, headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None):
        return self._request(self._session.get, url, headers=headers, params=params)

    def post(self, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None, data: Any = None):
        return self._request(self._session.post, url, headers=headers, json=json, data=data)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class CurlTransport:
    def open(self, profile: SessionProfile) -> CurlSession:
        return CurlSession(profile)
