import threading
from collections.abc import Callable
from urllib.parse import quote

from alt_ani_core.models import EpisodeRow
from alt_ani_core.testing import FakeRequest, FakeResponse, FakeTransport

TIMEOUT = 10.0
# long enough for a thread that is free to run to get there, short enough to keep the suite fast
QUIET = 0.2

TEST_TOKEN = "VGVzdFRva2VuRm9yVW5pdFRlc3RzT25seQ+/=="
OTHER_TOKEN = "T3RoZXJUb2tlbkZvclVuaXRUZXN0cw+/=="
TRAP_ID = "2045542847"
EPISODE_URL = "https://shinden.pl/episode/61064-sousou-no-frieren/view/227851"
EPISODE = EpisodeRow(number=1.0, title="Ep 1", url=EPISODE_URL)
EPISODE_2 = EpisodeRow(number=2.0, title="Ep 2", url="https://shinden.pl/episode/61064-sousou-no-frieren/view/227852")
INVALID_TOKEN = '{"error": {"code": "invalid_token", "message": "expired"}}'


def quoted(token: str) -> str:
    return quote(token, safe="")


def is_load(request: FakeRequest) -> bool:
    return "/player_load" in request.url


def is_show(request: FakeRequest) -> bool:
    return "/player_show" in request.url


def online_id_of(url: str) -> str:
    return url.split("/xhr/", 1)[1].split("/", 1)[0]


def embed_host(online_id: str) -> str:
    return f"h{online_id}.example"


def embed_url(online_id: str) -> str:
    return f"https://{embed_host(online_id)}/e/{online_id}"


def iframe(request: FakeRequest) -> str:
    return f'<iframe src="{embed_url(online_id_of(request.url))}"></iframe>'


def invalid_token() -> FakeResponse:
    return FakeResponse(text=INVALID_TOKEN, status_code=403)


def route_xhr(transport: FakeTransport, *, load="0", show=iframe) -> None:
    transport.route(is_load, load)
    transport.route(is_show, show)


def xhr_urls(transport: FakeTransport) -> list[str]:
    return [url for url in transport.urls if "/xhr/" in url]


def xhr_calls(transport: FakeTransport) -> list[tuple[str, str]]:
    """(online_id, stage) of every player_load/player_show request, in order."""
    return [(online_id_of(url), url.split("/xhr/", 1)[1].split("/", 1)[1].split("?")[0]) for url in xhr_urls(transport)]


def page_requests(transport: FakeTransport, url: str = EPISODE_URL) -> int:
    return transport.urls.count(url)


class Gate:
    """Holds a fake reply open until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def block(self) -> None:
        self.entered.set()
        assert self.release.wait(TIMEOUT), "the gate was never released"

    def wait_entered(self) -> None:
        assert self.entered.wait(TIMEOUT), "the gated request never started"


class Background:
    """Runs one call on a named thread and keeps what it returned or raised, EngineInterrupt included."""

    def __init__(self, name: str, call: Callable[[], object]) -> None:
        self.result: object = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._call = call
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.result = self._call()
        except BaseException as exc:  # noqa: B036
            self.error = exc
        finally:
            self.done.set()

    def join(self) -> None:
        assert self.done.wait(TIMEOUT), "the background call never finished"
        self._thread.join(TIMEOUT)


class Traffic:
    """Wraps replies to log which thread made each request and how many replies were in flight at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.log: list[tuple[str, str]] = []

    def wrap(self, reply):
        def _reply(request: FakeRequest):
            with self._lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)
                self.log.append((threading.current_thread().name, request.url))
            try:
                return reply(request) if callable(reply) else reply
            finally:
                with self._lock:
                    self._active -= 1

        return _reply

    def threads(self) -> list[str]:
        with self._lock:
            return [name for name, _url in self.log]

    def by(self, name: str) -> list[str]:
        with self._lock:
            return [url for thread, url in self.log if thread == name]
