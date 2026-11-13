"""Test doubles for code built on the engine; no test framework is imported here."""

import json as json_module
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

from alt_ani_core.cancel import CancelToken
from alt_ani_core.errors import OperationCancelled
from alt_ani_core.events import EngineEvent
from alt_ani_core.history.store import DownloadEntry, WatchEntry
from alt_ani_core.models import SeriesRef, Stream, StreamProtocol, StreamVariant
from alt_ani_core.transport.api import DecodeError, HttpStatusError, SessionProfile


@dataclass
class FakeResponse:
    text: str = ""
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""

    def json(self) -> Any:
        try:
            return json_module.loads(self.text)
        except ValueError:
            raise DecodeError(url=self.url) from None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpStatusError(self.status_code, url=self.url)


@dataclass(frozen=True)
class FakeRequest:
    method: str
    url: str
    profile: str
    headers: Mapping[str, str] | None = None
    params: Mapping[str, Any] | None = None
    json: Any = None
    data: Any = None


Outcome = FakeResponse | str | BaseException
Reply = Outcome | Callable[[FakeRequest], Outcome]


def _materialize(reply: Reply, request: FakeRequest) -> FakeResponse:
    outcome = reply if isinstance(reply, FakeResponse | str | BaseException) else reply(request)
    if isinstance(outcome, BaseException):
        raise outcome
    if isinstance(outcome, str):
        return FakeResponse(text=outcome, url=request.url)
    if not outcome.url:
        outcome.url = request.url
    return outcome


def _prefix(prefix: str) -> Callable[[FakeRequest], bool]:
    return lambda request: request.url.startswith(prefix)


class FakeSession:
    def __init__(self, transport: FakeTransport, profile: SessionProfile) -> None:
        self._transport = transport
        self.profile = profile
        self.closed = False

    def get(self, url: str, *, headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None):
        return self._transport.handle(FakeRequest("GET", url, self.profile.name, headers=headers, params=params))

    def post(self, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None, data: Any = None):
        return self._transport.handle(FakeRequest("POST", url, self.profile.name, headers=headers, json=json, data=data))

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeTransport:
    """Routes win over the script; a request matching neither fails the test instead of reaching the network."""

    def __init__(self) -> None:
        self.requests: list[FakeRequest] = []
        self.sessions: list[FakeSession] = []
        self._routes: list[tuple[Callable[[FakeRequest], bool], Reply]] = []
        self._script: list[Reply] = []
        self._lock = threading.Lock()

    def route(self, match: str | Callable[[FakeRequest], bool], reply: Reply) -> Self:
        """``match`` is a URL prefix or a predicate; later routes win, so a test can override a default."""
        predicate = _prefix(match) if isinstance(match, str) else match
        self._routes.insert(0, (predicate, reply))
        return self

    def script(self, *replies: Reply) -> Self:
        """Replies handed out in order to requests no route matches."""
        self._script.extend(replies)
        return self

    def open(self, profile: SessionProfile) -> FakeSession:
        session = FakeSession(self, profile)
        with self._lock:
            self.sessions.append(session)
        return session

    def handle(self, request: FakeRequest) -> FakeResponse:
        with self._lock:
            self.requests.append(request)
            reply = next((r for predicate, r in self._routes if predicate(request)), None)
            if reply is None:
                if not self._script:
                    raise AssertionError(f"unexpected {request.method} request in a test")
                reply = self._script.pop(0)
        return _materialize(reply, request)

    @property
    def urls(self) -> list[str]:
        return [request.url for request in self.requests]


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[EngineEvent] = []

    def emit(self, event: EngineEvent) -> None:
        self.events.append(event)

    def of[E: EngineEvent](self, kind: type[E]) -> list[E]:
        return [event for event in self.events if isinstance(event, kind)]


class RecordingCancel(CancelToken):
    """Never sleeps; records each requested wait and can cancel itself once a given number of waits began."""

    def __init__(self, *, cancel_on_wait: int | None = None) -> None:
        super().__init__()
        self.waits: list[float] = []
        self._cancel_on_wait = cancel_on_wait

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self._cancel_on_wait is not None and len(self.waits) >= self._cancel_on_wait:
            self.cancel()
        if self.cancelled:
            raise OperationCancelled()


class InMemoryHistory:
    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC).isoformat())
        self._series: dict[str, SeriesRef] = {}
        self._watch: dict[str, tuple[float, str]] = {}
        self._downloads: dict[str, tuple[set[float], str]] = {}

    def record_watch(self, series: SeriesRef, episode: float) -> None:
        self._series[series.id] = series
        self._watch[series.id] = (float(episode), self._clock())

    def record_download(self, series: SeriesRef, episode: float) -> None:
        self._series[series.id] = series
        episodes = self._downloads.get(series.id, (set(), ""))[0]
        episodes.add(float(episode))
        self._downloads[series.id] = (episodes, self._clock())

    def watch_entries(self) -> list[WatchEntry]:
        entries = [WatchEntry(self._series[sid], last_ep, at) for sid, (last_ep, at) in self._watch.items()]
        return sorted(entries, key=lambda entry: entry.updated_at, reverse=True)

    def download_entries(self) -> list[DownloadEntry]:
        entries = [DownloadEntry(self._series[sid], frozenset(eps), at) for sid, (eps, at) in self._downloads.items()]
        return sorted(entries, key=lambda entry: entry.updated_at, reverse=True)

    def clear(self) -> None:
        self._series.clear()
        self._watch.clear()
        self._downloads.clear()


def make_stream(
    url: str = "https://cdn.example/video.mp4",
    headers: Mapping[str, str] | None = None,
    qualities: Mapping[str, str] | None = None,
    ext: str = "mp4",
) -> Stream:
    """A stream in the old flat shape: one default URL plus optional label→URL renditions."""
    protocol = StreamProtocol.HLS if ext == "m3u8" else StreamProtocol.PROGRESSIVE
    container = None if protocol is StreamProtocol.HLS else ext
    shared = dict(headers or {})
    if not qualities:
        return Stream.single(url, protocol, shared, container)
    variants = tuple(
        StreamVariant(url=variant_url, protocol=protocol, headers=shared, label=label, container=container)
        for label, variant_url in qualities.items()
    )
    default = next((i for i, variant in enumerate(variants) if variant.url == url), None)
    if default is None:
        variants = (StreamVariant(url=url, protocol=protocol, headers=shared, container=container), *variants)
        default = 0
    return Stream(variants=variants, default_index=default)
