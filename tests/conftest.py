import logging
import socket
import threading
from collections.abc import Iterator
from contextlib import suppress
from urllib.parse import urlsplit

import pytest
from curl_cffi.const import CurlInfo, CurlOpt
from curl_cffi.curl import Curl

from alt_ani_cli import paths
from alt_ani_cli.paths import CliPaths
from alt_ani_core import diagnostics
from alt_ani_core.config import EngineConfig
from alt_ani_core.services.engine import Engine
from alt_ani_core.testing import FakeTransport, InMemoryHistory, RecordingSink


@pytest.fixture(autouse=True)
def cli_paths(tmp_path, monkeypatch) -> CliPaths:
    """Every test gets its own state, cache and download directories; the real user profile is never touched."""
    isolated = CliPaths(state_dir=tmp_path / "state", cache_dir=tmp_path / "cache", downloads_dir=tmp_path / "videos")
    monkeypatch.setattr(paths, "load_paths", lambda: isolated)
    return isolated


@pytest.fixture(autouse=True)
def _close_diagnostics_log():
    yield
    for handler in [h for h in diagnostics._logger.handlers if not isinstance(h, logging.NullHandler)]:
        diagnostics._logger.removeHandler(handler)
        handler.close()


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(scope="session")
def _sinkhole_port() -> Iterator[int]:
    """A local listener that drops every connection: the proxy libcurl gets for any host that is not loopback."""
    listener = socket.create_server(("127.0.0.1", 0))

    def drop() -> None:
        with suppress(OSError):
            while True:
                conn, _ = listener.accept()
                conn.close()

    threading.Thread(target=drop, daemon=True).start()
    yield listener.getsockname()[1]
    listener.close()


def _loopback_curl(real_perform, sinkhole_port: int):
    """libcurl bypasses Python sockets and follows redirects itself; only loopback hosts skip the dead proxy."""

    def perform(curl, *args, **kwargs):
        curl.setopt(CurlOpt.PROXY, f"http://127.0.0.1:{sinkhole_port}")
        curl.setopt(CurlOpt.NOPROXY, ",".join(LOOPBACK_HOSTS))
        try:
            return real_perform(curl, *args, **kwargs)
        finally:
            url = curl.getinfo(CurlInfo.EFFECTIVE_URL)
            host = urlsplit(url.decode() if isinstance(url, bytes) else str(url)).hostname
            if host not in LOOPBACK_HOSTS:
                raise AssertionError("non-loopback network attempted in a loopback test")

    return perform


@pytest.fixture(autouse=True)
def _no_outbound_network(request, monkeypatch):
    """Fail fast if a missing fake would let a unit test reach libcurl or a raw socket."""
    if request.node.get_closest_marker("integration"):
        return

    def _blocked(*args, **kwargs):
        raise AssertionError("outbound network attempted in a unit test")

    if request.node.get_closest_marker("loopback"):
        real_connect = socket.socket.connect

        def _loopback_only(sock, address):
            host = address[0] if isinstance(address, tuple) else address
            if host not in LOOPBACK_HOSTS:
                raise AssertionError("non-loopback network attempted in a loopback test")
            return real_connect(sock, address)

        monkeypatch.setattr(socket.socket, "connect", _loopback_only)
        monkeypatch.setattr(Curl, "perform", _loopback_curl(Curl.perform, request.getfixturevalue("_sinkhole_port")))
        return
    monkeypatch.setattr(Curl, "perform", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def make_engine(tmp_path, transport, sink):
    """Engine on the fake transport with in-memory history; antibot wait is 0 unless a test asks otherwise."""
    engines: list[Engine] = []

    def _make(*, transport=transport, history=None, events=sink, clock=None, **config) -> Engine:
        config.setdefault("antibot_delay_sec", 0.0)
        kwargs = {"clock": clock} if clock is not None else {}
        engine = Engine(
            EngineConfig(cache_dir=tmp_path / "cache", **config),
            transport=transport,
            history=history if history is not None else InMemoryHistory(),
            events=events,
            **kwargs,
        )
        engines.append(engine)
        return engine

    yield _make
    for engine in engines:
        engine.close()


@pytest.fixture
def player_show_html():
    return '<html><body><iframe src="//video.sibnet.ru/shell.php?videoid=1234567"></iframe></body></html>'
