"""Controlled server for Android playback and download tests; run it, then ``adb reverse tcp:8765 tcp:8765``."""

import argparse
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "shinden"
MEDIA = Path(__file__).resolve().parent / "media"
HOST = "controlled.test"
USER_AGENT = "alt-ani-controlled/1"
REQUIRED_HEADERS = {"Referer": f"https://{HOST}/", "User-Agent": USER_AGENT, "Origin": f"https://{HOST}"}
PAGE_TOKEN = "Q29udHJvbGxlZFNlcnZlclRva2Vu"
_ISLAND = '<div class="ep-players__island" data-sh-module="episode-players">'
_KINDS = {"progressive": "video.mp4", "hls": "hls/index.m3u8", "dash": "dash/manifest.mpd"}
_TYPES = {".mp4": "video/mp4", ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t", ".mpd": "application/dash+xml"}
_SEGMENTS = (".ts", ".m4s")
_LIVE_PLAYLIST = "hls/index.m3u8"
_LIVE_START_SEGMENTS = 3
_SEGMENT_SEC = 2
_SEGMENT_COUNT = 15
_CHUNK = 1024
_STALL_LIMIT_SEC = 120


@dataclass
class Scenario:
    """Labels share one media file; ``refresh_*`` apply from the second resolve, ``stall_files`` hold until released."""

    kind: str = "hls"
    variants: list[str] = field(default_factory=lambda: ["720p", "480p"])
    refresh_variants: list[str] | None = None
    ttl_sec: float | None = None
    refresh_ttl_sec: float | None = None
    segment_delay_ms: int = 0
    require_headers: bool = True
    live: bool = False
    throttle_bps: int | None = None
    fail_files: dict[str, int] = field(default_factory=dict)
    redirect_files: list[str] = field(default_factory=list)
    redirect_delay_ms: int = 0
    etag: bool = False
    media_revision: int = 0
    stall_files: dict[str, int] = field(default_factory=dict)
    stall_skip: int = 0
    pad_bytes: int = 0


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.secret = b"controlled-server-secret"
        self.scenario = Scenario()
        self.resolves = 0
        self.requests: list[dict[str, Any]] = []
        self.failures: dict[str, int] = {}
        self.live_started: float | None = None
        self.issued: set[int] = set()
        self.revoked: set[int] = set()
        self.released = threading.Event()
        self.stall_seen: dict[str, int] = {}

    def reset(self, scenario: Scenario) -> None:
        with self.lock:
            self.scenario = scenario
            self.resolves = 0
            self.requests = []
            self.failures = {}
            self.live_started = None
            self.issued = set()
            self.revoked = set()
            # a response still held by the previous scenario goes on, so no server thread waits for a test that ended
            self.released.set()
            self.released = threading.Event()
            self.stall_seen = {}

    def release(self) -> None:
        with self.lock:
            self.released.set()

    def should_stall(self, name: str) -> bool:
        with self.lock:
            if name not in self.scenario.stall_files or self.released.is_set():
                return False
            self.stall_seen[name] = self.stall_seen.get(name, 0) + 1
            return self.stall_seen[name] > self.scenario.stall_skip

    def etag_of(self, body: bytes) -> str | None:
        with self.lock:
            scenario = self.scenario
        if not scenario.etag:
            return None
        return f'"{hashlib.sha256(body).hexdigest()[:16]}-r{scenario.media_revision}"'

    def expire_issued(self) -> int:
        """Every URL handed out so far answers 403 ``expired`` from now on; later resolves get working ones."""
        with self.lock:
            self.revoked |= self.issued
            return len(self.revoked)

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            entry["seq"] = len(self.requests) + 1
            self.requests.append(entry)

    def update(self, entry: dict[str, Any], **fields: Any) -> None:
        with self.lock:
            entry.update(fields)

    def inject_failure(self, name: str) -> bool:
        """The first ``fail_files[name]`` requests for a file get a 503."""
        with self.lock:
            used = self.failures.get(name, 0)
            if used >= self.scenario.fail_files.get(name, 0):
                return False
            self.failures[name] = used + 1
            return True

    def live_playlist(self) -> bytes:
        """An EVENT playlist that gains a segment every two seconds after its first request, then ends."""
        with self.lock:
            if self.live_started is None:
                self.live_started = time.monotonic()
            elapsed = time.monotonic() - self.live_started
        count = min(_SEGMENT_COUNT, _LIVE_START_SEGMENTS + int(elapsed // _SEGMENT_SEC))
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{_SEGMENT_SEC}", "#EXT-X-MEDIA-SEQUENCE:0"]
        lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
        for index in range(count):
            lines += [f"#EXTINF:{_SEGMENT_SEC}.000000,", f"seg_{index:03d}.ts"]
        if count == _SEGMENT_COUNT:
            lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode()

    def sign(self, exp: int) -> str:
        return hmac.new(self.secret, str(exp).encode(), hashlib.sha256).hexdigest()[:16]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            requests = [dict(entry) for entry in self.requests]
            return {"scenario": asdict(self.scenario), "resolves": self.resolves, "requests": requests}


def episode_page() -> str:
    html = (FIXTURES / "episode_players.html").read_text(encoding="utf-8")
    return html.replace(_ISLAND, _ISLAND[:-1] + f' data-player-auth="{PAGE_TOKEN}">', 1)


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/m/"):
            self._media(path, head=True)
        else:
            self._send(404, b"", head=True)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/m/"):
            self._media(path)
        elif path.startswith("/shinden/"):
            self._shinden(path.removeprefix("/shinden"))
        elif path.startswith("/host/api/"):
            self._host_api(path.removeprefix("/host/api/"))
        elif path == "/_control/log":
            self._send(200, json.dumps(self.server.state.snapshot()).encode(), "application/json")
        else:
            self._send(404, b"")

    def do_POST(self) -> None:
        # read even when unused: a body left behind would be parsed as the next request on this keep-alive connection
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if urlsplit(self.path).path == "/_control/expire":
            revoked = self.server.state.expire_issued()
            self._send(200, json.dumps({"revoked": revoked}).encode(), "application/json")
            return
        if urlsplit(self.path).path == "/_control/release":
            self.server.state.release()
            self._send(200, b"{}", "application/json")
            return
        if urlsplit(self.path).path != "/_control/scenario":
            self._send(404, b"")
            return
        try:
            scenario = Scenario(**json.loads(body or b"{}"))
        except (TypeError, ValueError) as exc:
            self._send(400, str(exc).encode())
            return
        if scenario.kind not in _KINDS:
            self._send(400, b"unknown kind")
            return
        self.server.state.reset(scenario)
        self._send(200, json.dumps(asdict(scenario)).encode(), "application/json")

    def _shinden(self, path: str) -> None:
        if path.startswith("/xhr/") and path.endswith("/player_load"):
            self._send(200, b"0", "text/plain")
        elif path.startswith("/xhr/") and path.endswith("/player_show"):
            online_id = path.split("/")[2]
            self._send(200, f'<iframe src="//{HOST}/e/{online_id}"></iframe>'.encode(), "text/html")
        elif path.startswith("/episode/"):
            self._send(200, episode_page().encode(), "text/html; charset=utf-8")
        elif path.startswith("/series/") and path.endswith("/episodes"):
            self._send(200, (FIXTURES / "episodes_frieren.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/series":
            self._send(200, (FIXTURES / "search_frieren.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"")

    def _host_api(self, online_id: str) -> None:
        state = self.server.state
        with state.lock:
            state.resolves += 1
            scenario = state.scenario
            refreshed = state.resolves > 1
        labels = scenario.refresh_variants if refreshed and scenario.refresh_variants else scenario.variants
        ttl = scenario.refresh_ttl_sec if refreshed else scenario.ttl_sec
        exp = int(time.time() + ttl) if ttl else 0
        with state.lock:
            state.issued.add(exp)
        base = f"http://{self.headers.get('Host')}/m/{exp}/{state.sign(exp)}/{_KINDS[scenario.kind]}"
        headers = dict(REQUIRED_HEADERS, Referer=f"https://{HOST}/e/{online_id}")
        variants = [
            {"label": label, "url": f"{base}?q={label}", "protocol": scenario.kind, "headers": headers} for label in labels
        ]
        self._send(200, json.dumps({"variants": variants}).encode(), "application/json")

    def _media(self, path: str, head: bool = False) -> None:
        state = self.server.state
        _, _, exp_text, sig, *rest = path.split("/")
        redirected = rest[:1] == ["r"]
        name = "/".join(rest[1:] if redirected else rest)
        with state.lock:
            scenario = state.scenario
        missing = [h for h in REQUIRED_HEADERS if not self._header_ok(h)] if scenario.require_headers else []
        entry: dict[str, Any] = {
            "method": "HEAD" if head else "GET",
            "file": name,
            "missing": missing,
            "range": self.headers.get("Range"),
            "at": time.time(),
            "probe": self.headers.get("X-Probe-Seq"),
            "conn": self.client_address[1],
            "redirected": redirected,
        }
        target = (MEDIA / name).resolve()
        if not exp_text.isdigit() or not hmac.compare_digest(sig, state.sign(int(exp_text))):
            status = 403
            entry["reason"] = "signature"
        elif int(exp_text) in state.revoked or (int(exp_text) and time.time() > int(exp_text)):
            status = 403
            entry["reason"] = "expired"
        elif missing:
            status = 403
            entry["reason"] = "headers"
        elif MEDIA not in target.parents or not target.is_file():
            status = 404
        elif state.inject_failure(name):
            status = 503
            entry["reason"] = "injected"
        elif name in scenario.redirect_files and not redirected:
            status = 302
        else:
            status = 200
        entry["status"] = status
        state.record(entry)
        if status == 302:
            # held back so a test can close its transfer gate while this request is still being answered
            time.sleep(scenario.redirect_delay_ms / 1000)
            self._send(302, b"", extra={"Location": f"/m/{exp_text}/{sig}/r/{name}"})
            state.update(entry, done_at=time.time())
            return
        if status != 200:
            self._send(status, b"", head=head)
            return
        if target.suffix in _SEGMENTS and scenario.segment_delay_ms and not head:
            time.sleep(scenario.segment_delay_ms / 1000)
        live = scenario.live and name == _LIVE_PLAYLIST
        body = state.live_playlist() if live else target.read_bytes()
        if name == _KINDS["progressive"]:
            body += bytes(scenario.pad_bytes)
        throttle = scenario.throttle_bps if target.suffix in _SEGMENTS else None
        etag = None if live else state.etag_of(body)
        validator = {"ETag": etag} if etag else {}
        requested = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if if_range is not None and if_range != etag:
            # RFC 9110: a validator that no longer matches turns the range request into a full one
            requested = None
        content_type = _TYPES.get(target.suffix, "video/mp4")
        stall = None
        if not head and state.should_stall(name):
            stall = (scenario.stall_files[name], state.released, lambda: state.update(entry, stalled=True))
        if requested and requested.startswith("bytes="):
            first, _, last = requested.removeprefix("bytes=").partition("-")
            start, end = int(first or 0), int(last) if last else len(body) - 1
            extra = {"Content-Range": f"bytes {start}-{end}/{len(body)}", **validator}
            sent = self._send(206, body[start : end + 1], content_type, extra, throttle, head=head, stall=stall)
        else:
            extra = {"Accept-Ranges": "bytes", **validator}
            sent = self._send(200, body, content_type, extra, throttle, head=head, stall=stall)
        state.update(entry, done_at=time.time(), sent=sent, size=len(body), aborted=sent is None)

    def _header_ok(self, name: str) -> bool:
        value = self.headers.get(name)
        if name == "Referer":
            return value is not None and value.startswith(REQUIRED_HEADERS["Referer"])
        return value == REQUIRED_HEADERS[name]

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/plain",
        extra: dict[str, str] | None = None,
        throttle_bps: int | None = None,
        *,
        head: bool = False,
        stall: tuple[int, threading.Event, Callable[[], None]] | None = None,
    ) -> int | None:
        """The number of body bytes written, or None when the client went away first."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if head:
            return 0
        try:
            if stall is not None:
                after, released, mark = stall
                self.wfile.write(body[:after])
                self.wfile.flush()
                mark()
                released.wait(_STALL_LIMIT_SEC)
                self.wfile.write(body[after:])
                return len(body)
            if not throttle_bps:
                self.wfile.write(body)
                return len(body)
            for offset in range(0, len(body), _CHUNK):
                self.wfile.write(body[offset : offset + _CHUNK])
                self.wfile.flush()
                time.sleep(_CHUNK / throttle_bps)
        except BrokenPipeError, ConnectionResetError, ConnectionAbortedError:
            self.close_connection = True
            return None
        return len(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.state = State()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with Server(args.port) as server:
        print(f"controlled server on {server.url}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
