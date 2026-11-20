"""The autouse network guard itself: loopback tests reach only their own server, through libcurl as well."""

import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from curl_cffi import requests as curl_requests

# TEST-NET-2 (RFC 5737): never routed, so even a leak would reach no one
OUTSIDE = "http://198.51.100.7/escaped"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/away":
            self.send_response(302)
            self.send_header("Location", OUTSIDE)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@contextmanager
def _serve() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.unit
@pytest.mark.loopback
class TestLoopbackGuard:
    def test_libcurl_reaches_the_local_server(self):
        with _serve() as url:
            assert curl_requests.get(f"{url}/here", timeout=5).content == b"ok"

    def test_libcurl_cannot_leave_loopback(self):
        with pytest.raises(AssertionError, match="non-loopback"):
            curl_requests.get(OUTSIDE, timeout=5)

    def test_libcurl_redirect_off_loopback_is_stopped(self):
        with _serve() as url, pytest.raises(AssertionError, match="non-loopback"):
            curl_requests.get(f"{url}/away", timeout=5, allow_redirects=True)

    def test_python_redirect_off_loopback_is_stopped(self):
        with _serve() as url, pytest.raises(AssertionError, match="non-loopback"):
            urllib.request.urlopen(f"{url}/away", timeout=5)


@pytest.mark.unit
class TestUnitGuard:
    def test_libcurl_is_blocked_even_for_loopback(self):
        with pytest.raises(AssertionError, match="outbound network"):
            curl_requests.get("http://127.0.0.1:9/", timeout=5)
