"""Local HTTP server for offline-executor tests: ranges, ETags, content changes, stalls and HLS, no hosting."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = "SECRETTOKEN0123"


@dataclass
class Resource:
    body: bytes
    etag: str | None = None
    content_type: str = "video/mp4"


@dataclass
class ServerState:
    resources: dict[str, Resource] = field(default_factory=dict)
    ignore_range: bool = False
    head_allowed: bool = True
    # a status, or "drop" to close the connection unanswered
    head_failure: int | str | None = None
    stall_after: int | None = None
    stall: threading.Event = field(default_factory=threading.Event)
    log: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)
    on_request: object = None
    forbidden_query: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, *_args) -> None:
        pass

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, *, head: bool) -> None:
        state = self.server.state
        path = self.path.split("?", 1)[0]
        with state.lock:
            state.log.append((self.command, path, self.headers.get("Range"), self.headers.get("If-Range")))
            if callable(state.on_request):
                state.on_request(self.command, path)
            resource = state.resources.get(path)
        if state.forbidden_query and state.forbidden_query in self.path:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if head and state.head_failure == "drop":
            self.close_connection = True
            return
        if head and isinstance(state.head_failure, int):
            self.send_response(state.head_failure)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if resource is None or (head and not state.head_allowed):
            self.send_response(404 if resource is None else 405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, status, start = resource.body, 200, 0
        requested = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if requested and not state.ignore_range and (if_range is None or if_range == resource.etag):
            start = int(requested.split("=")[1].split("-")[0])
            status = 206
        payload = body[start:]
        self.send_response(status)
        self.send_header("Content-Type", resource.content_type)
        self.send_header("Content-Length", str(len(payload)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        if resource.etag:
            self.send_header("ETag", resource.etag)
        self.end_headers()
        if head:
            return
        if state.stall_after is not None and resource.content_type.startswith("video"):
            self.wfile.write(payload[: state.stall_after])
            self.wfile.flush()
            state.stall.wait(30)
            return
        self.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: ServerState) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state


@contextmanager
def serve(state: ServerState) -> Iterator[str]:
    server = _Server(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        state.stall.set()
        server.shutdown()
        server.server_close()


def random_body(size: int) -> bytes:
    return os.urandom(size)


def hls(state: ServerState, *, segments: int = 6, seg_size: int = 18800, etags: bool = True, key: bool = False) -> bytes:
    """A media playlist with token-bearing segment URIs; returns the concatenated segment bytes."""
    bodies = [bytes([0x47]) + os.urandom(seg_size - 1) for _ in range(segments)]
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:0"]
    if key:
        lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key.bin"')
    for i in range(segments):
        lines += ["#EXTINF:4.0,", f"seg{i}.ts?tok={SECRET}"]
        state.resources[f"/seg{i}.ts"] = Resource(bodies[i], f'"s{i}-{SECRET}"' if etags else None, "video/mp2t")
    lines.append("#EXT-X-ENDLIST")
    state.resources["/v.m3u8"] = Resource("\n".join(lines).encode(), None, "application/vnd.apple.mpegurl")
    return b"".join(bodies)


def replace_segment(state: ServerState, index: int) -> None:
    old = state.resources[f"/seg{index}.ts"]
    state.resources[f"/seg{index}.ts"] = Resource(
        bytes([0x47]) + os.urandom(len(old.body) - 1), f'"changed{index}"', old.content_type
    )
