"""Controlled server features the device tests rely on: probes, failures, redirects, live, throttling, validators, held I/O."""

import http.client
import json
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


def test_expire_revokes_the_urls_handed_out_so_far_only(server):
    server.state.reset(Scenario(kind="hls", ttl_sec=600))
    resolve = f"{server.url}/host/api/1"
    first = json.loads(fetch(resolve)[1])["variants"][0]["url"]
    assert fetch(first)[0] == 200
    expire = urllib.request.Request(f"{server.url}/_control/expire", data=b"{}", method="POST")
    with urllib.request.urlopen(expire, timeout=30) as reply:
        assert json.loads(reply.read()) == {"revoked": 1}
    assert fetch(first)[0] == 403
    assert requests(server)[-1]["reason"] == "expired"
    second = json.loads(fetch(resolve)[1])["variants"][0]["url"]
    assert fetch(second)[0] == 200


def test_a_control_post_leaves_a_keep_alive_connection_usable(server):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    try:
        connection.request("POST", "/_control/expire", body=b"{}", headers={"Content-Type": "application/json"})
        connection.getresponse().read()
        connection.request("GET", "/_control/log", headers={"Connection": "close"})
        assert connection.getresponse().status == 200
    finally:
        connection.close()


def head(url: str) -> tuple[int, dict[str, str]]:
    request = urllib.request.Request(url, headers=REQUIRED_HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return reply.status, dict(reply.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def fetch_with_headers(url: str, **extra: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={**REQUIRED_HEADERS, **extra})
    with urllib.request.urlopen(request, timeout=30) as reply:
        return reply.status, dict(reply.headers), reply.read()


def test_head_answers_the_size_and_a_strong_etag_without_a_body(server):
    server.state.reset(Scenario(kind="progressive", etag=True))
    status, headers = head(media_url(server, "video.mp4"))
    assert status == 200
    assert headers["Content-Length"] == str((MEDIA / "video.mp4").stat().st_size)
    assert headers["ETag"].startswith('"') and headers["ETag"].endswith('-r0"')
    (entry,) = finished(server)
    assert (entry["method"], entry["sent"]) == ("HEAD", 0)


def test_without_the_etag_switch_no_validator_is_sent(server):
    server.state.reset(Scenario(kind="progressive"))
    assert "ETag" not in head(media_url(server, "video.mp4"))[1]


def test_a_new_media_revision_changes_the_etag_and_a_stale_if_range_gets_the_whole_file(server):
    url = media_url(server, "video.mp4")
    body = (MEDIA / "video.mp4").read_bytes()
    server.state.reset(Scenario(kind="progressive", etag=True))
    old = head(url)[1]["ETag"]
    status, headers, part = fetch_with_headers(url, Range="bytes=100-", **{"If-Range": old})
    assert (status, headers["ETag"], part) == (206, old, body[100:])
    server.state.reset(Scenario(kind="progressive", etag=True, media_revision=1))
    status, headers, whole = fetch_with_headers(url, Range="bytes=100-", **{"If-Range": old})
    assert (status, whole) == (200, body)
    assert headers["ETag"] != old


def test_a_stalled_file_holds_its_rest_until_released_and_marks_the_log(server):
    server.state.reset(Scenario(kind="progressive", stall_files={"video.mp4": 4096}))
    result: dict = {}
    reader = threading.Thread(target=lambda: result.update(reply=fetch(media_url(server, "video.mp4"))))
    reader.start()
    deadline = time.monotonic() + 10
    while not any(e.get("stalled") for e in requests(server)):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    reader.join(0.3)
    assert reader.is_alive(), "the body must not finish while the file is held"
    server.state.release()
    reader.join(10)
    assert result["reply"] == (200, (MEDIA / "video.mp4").read_bytes())
    assert fetch(media_url(server, "video.mp4"))[0] == 200


def test_a_new_scenario_lets_a_held_response_finish(server):
    server.state.reset(Scenario(kind="progressive", stall_files={"video.mp4": 1024}))
    reader = threading.Thread(target=lambda: fetch(media_url(server, "video.mp4")))
    reader.start()
    deadline = time.monotonic() + 10
    while not any(e.get("stalled") for e in requests(server)):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    server.state.reset(Scenario(kind="progressive"))
    reader.join(10)
    assert not reader.is_alive()


def test_release_is_a_control_endpoint(server):
    server.state.reset(Scenario(kind="progressive", stall_files={"video.mp4": 1}))
    release = urllib.request.Request(f"{server.url}/_control/release", data=b"{}", method="POST")
    with urllib.request.urlopen(release, timeout=30) as reply:
        assert reply.status == 200
    assert server.state.released.is_set()
