"""Tests for extract/mp4upload.py — stream URL resolver."""

import base64
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.mp4upload import resolve

_EMBED = "https://www.mp4upload.com/embed-abc123.html"
_REFERER = "https://shinden.pl/"


def _make_session_patch(html: str):
    """Return a context manager that patches cffi_requests.Session to serve html."""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    resp.raise_for_status.return_value = None

    session = MagicMock()
    session.get.return_value = resp
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.mp4upload.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolveMp4upload:
    def test_player_src_pattern(self):
        with _make_session_patch("<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>"):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == "https://cdn.mp4upload.com/v/video.mp4"
        assert stream.ext == "mp4"

    def test_file_key_pattern(self):
        with _make_session_patch('<script>var player = {file: "https://cdn.mp4upload.com/v/video.mp4"};</script>'):
            stream = resolve(_EMBED, _REFERER)
        assert "mp4upload" in stream.url or stream.url.endswith(".mp4")

    def test_m3u8_ext_detection(self):
        with _make_session_patch("<script>player.src('https://cdn.mp4upload.com/v/playlist.m3u8');</script>"):
            stream = resolve(_EMBED, _REFERER)
        assert stream.ext == "m3u8"

    def test_base64_encoded_url(self):
        raw_url = "https://cdn.mp4upload.com/v/encoded_video.mp4"
        encoded = base64.b64encode(raw_url.encode()).decode()
        with _make_session_patch(f'<script>var player = {{"file": "{encoded}"}};</script>'):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == raw_url

    def test_raises_when_no_url_found(self):
        with _make_session_patch("<html><body>no video here</body></html>"):
            with pytest.raises(ValueError, match="mp4upload"):
                resolve(_EMBED, _REFERER)

    def test_referer_in_stream_headers(self):
        with _make_session_patch("<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>"):
            stream = resolve(_EMBED, _REFERER)
        assert stream.headers.get("Referer") == _EMBED
