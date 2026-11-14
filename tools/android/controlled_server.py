"""Controlled server for Android playback tests; run it, then ``adb reverse tcp:8765 tcp:8765``."""

import argparse
import hashlib
import hmac
import json
import threading
import time
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


@dataclass
class Scenario:
    """``variants`` are quality labels, all backed by the same media; ``refresh_*`` apply from the second resolve on."""

    kind: str = "hls"
    variants: list[str] = field(default_factory=lambda: ["720p", "480p"])
    refresh_variants: list[str] | None = None
    ttl_sec: float | None = None
    refresh_ttl_sec: float | None = None
    segment_delay_ms: int = 0
    require_headers: bool = True


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.secret = b"controlled-server-secret"
        self.scenario = Scenario()
        self.resolves = 0
        self.requests: list[dict[str, Any]] = []

    def reset(self, scenario: Scenario) -> None:
        with self.lock:
            self.scenario = scenario
            self.resolves = 0
            self.requests = []

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            entry["seq"] = len(self.requests) + 1
            self.requests.append(entry)

    def sign(self, exp: int) -> str:
        return hmac.new(self.secret, str(exp).encode(), hashlib.sha256).hexdigest()[:16]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"scenario": asdict(self.scenario), "resolves": self.resolves, "requests": list(self.requests)}


def episode_page() -> str:
    html = (FIXTURES / "episode_players.html").read_text(encoding="utf-8")
    return html.replace(_ISLAND, _ISLAND[:-1] + f' data-player-auth="{PAGE_TOKEN}">', 1)


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

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
        if urlsplit(self.path).path != "/_control/scenario":
            self._send(404, b"")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            scenario = Scenario(**json.loads(self.rfile.read(length) or b"{}"))
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
        base = f"http://{self.headers.get('Host')}/m/{exp}/{state.sign(exp)}/{_KINDS[scenario.kind]}"
        headers = dict(REQUIRED_HEADERS, Referer=f"https://{HOST}/e/{online_id}")
        variants = [
            {"label": label, "url": f"{base}?q={label}", "protocol": scenario.kind, "headers": headers} for label in labels
        ]
        self._send(200, json.dumps({"variants": variants}).encode(), "application/json")

    def _media(self, path: str) -> None:
        state = self.server.state
        _, _, exp_text, sig, *rest = path.split("/")
        name = "/".join(rest)
        with state.lock:
            scenario = state.scenario
        missing = [h for h in REQUIRED_HEADERS if not self._header_ok(h)] if scenario.require_headers else []
        entry: dict[str, Any] = {"file": name, "missing": missing, "range": self.headers.get("Range"), "at": time.time()}
        target = (MEDIA / name).resolve()
        if not exp_text.isdigit() or not hmac.compare_digest(sig, state.sign(int(exp_text))):
            status = 403
            entry["reason"] = "signature"
        elif int(exp_text) and time.time() > int(exp_text):
            status = 403
            entry["reason"] = "expired"
        elif missing:
            status = 403
            entry["reason"] = "headers"
        elif MEDIA not in target.parents or not target.is_file():
            status = 404
        else:
            status = 200
        entry["status"] = status
        state.record(entry)
        if status != 200:
            self._send(status, b"")
            return
        if target.suffix in _SEGMENTS and scenario.segment_delay_ms:
            time.sleep(scenario.segment_delay_ms / 1000)
        body = target.read_bytes()
        requested = self.headers.get("Range")
        content_type = _TYPES.get(target.suffix, "video/mp4")
        if requested and requested.startswith("bytes="):
            first, _, last = requested.removeprefix("bytes=").partition("-")
            start, end = int(first or 0), int(last) if last else len(body) - 1
            self._send(206, body[start : end + 1], content_type, {"Content-Range": f"bytes {start}-{end}/{len(body)}"})
        else:
            self._send(200, body, content_type, {"Accept-Ranges": "bytes"})

    def _header_ok(self, name: str) -> bool:
        value = self.headers.get(name)
        if name == "Referer":
            return value is not None and value.startswith(REQUIRED_HEADERS["Referer"])
        return value == REQUIRED_HEADERS[name]

    def _send(self, status: int, body: bytes, content_type: str = "text/plain", extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


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
