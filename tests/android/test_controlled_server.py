"""Controlled server features the T9 transfer-gate trial relies on: probes, injected failures, redirects, live, throttling."""

import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.android.controlled_server import MEDIA, REQUIRED_HEADERS, Scenario, Server  # noqa: E402

pytestmark = pytest.mark.loopback


@pytest.fixture
def server():
    with Server(0) as srv:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield srv
        srv.shutdown()


def media_url(server: Server, name: str) -> str:
    return f"{server.url}/m/0/{server.state.sign(0)}/{name}"


def fetch(url: str, **extra: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={**REQUIRED_HEADERS, **extra})
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def requests(server: Server) -> list[dict]:
    return server.state.snapshot()["requests"]


def finished(server: Server) -> list[dict]:
    """The log once the server has finished answering every request in it."""
    deadline = time.monotonic() + 15
    while not all("done_at" in entry or entry["status"] not in (200, 206, 302) for entry in requests(server)):
        assert time.monotonic() < deadline, requests(server)
        time.sleep(0.05)
    return requests(server)


def test_each_request_logs_its_probe_number_and_connection(server):
    server.state.reset(Scenario(kind="hls"))
    assert fetch(media_url(server, "hls/seg_000.ts"), **{"X-Probe-Seq": "7"})[0] == 200
    (entry,) = finished(server)
    assert entry["probe"] == "7"
    assert isinstance(entry["conn"], int)
    assert entry["sent"] == entry["size"] == (MEDIA / "hls" / "seg_000.ts").stat().st_size
    assert entry["aborted"] is False


def test_injected_failures_answer_503_the_given_number_of_times_then_serve_the_file(server):
    server.state.reset(Scenario(kind="hls", fail_files={"hls/seg_001.ts": 2}))
    url = media_url(server, "hls/seg_001.ts")
    assert [fetch(url)[0] for _ in range(3)] == [503, 503, 200]
    assert [e.get("reason") for e in requests(server)] == ["injected", "injected", None]
    assert fetch(media_url(server, "hls/seg_002.ts"))[0] == 200


def test_a_redirected_file_is_held_then_sent_to_the_same_file_with_the_probe_header_kept(server):
    server.state.reset(Scenario(kind="hls", redirect_files=["hls/seg_003.ts"], redirect_delay_ms=300))
    started = time.monotonic()
    status, body = fetch(media_url(server, "hls/seg_003.ts"), **{"X-Probe-Seq": "3"})
    assert status == 200
    assert body == (MEDIA / "hls" / "seg_003.ts").read_bytes()
    assert time.monotonic() - started >= 0.3
    first, second = finished(server)
    assert (first["status"], first["redirected"], second["status"], second["redirected"]) == (302, False, 200, True)
    assert first["probe"] == second["probe"] == "3"
    assert second["file"] == "hls/seg_003.ts"


def test_the_live_playlist_grows_every_two_seconds_and_ends_with_the_last_segment(server):
    server.state.reset(Scenario(kind="hls", live=True))
    url = media_url(server, "hls/index.m3u8")
    first = fetch(url)[1].decode()
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in first
    assert first.count("#EXTINF") == 3
    assert "#EXT-X-ENDLIST" not in first
    server.state.live_started = time.monotonic() - 4.5
    assert fetch(url)[1].decode().count("#EXTINF") == 5
    server.state.live_started = time.monotonic() - 60
    last = fetch(url)[1].decode()
    assert last.count("#EXTINF") == 15
    assert last.rstrip().endswith("#EXT-X-ENDLIST")


def test_a_throttled_segment_takes_its_size_over_the_rate(server):
    size = (MEDIA / "hls" / "seg_000.ts").stat().st_size
    server.state.reset(Scenario(kind="hls", throttle_bps=size * 2))
    started = time.monotonic()
    assert fetch(media_url(server, "hls/seg_000.ts"))[0] == 200
    assert time.monotonic() - started >= 0.4
    (entry,) = finished(server)
    assert entry["sent"] == size
    assert entry["done_at"] >= entry["at"]


def test_a_client_that_leaves_during_a_throttled_body_is_logged_as_aborted(server):
    server.state.reset(Scenario(kind="hls", throttle_bps=2048))
    path = media_url(server, "hls/seg_000.ts").removeprefix(server.url)
    host, port = server.server_address[:2]
    with socket.create_connection((host, port), timeout=10) as client:
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}"] + [f"{k}: {v}" for k, v in REQUIRED_HEADERS.items()]
        client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        client.recv(512)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    (entry,) = finished(server)
    assert entry["aborted"] is True
    assert entry["sent"] is None
