"""Tests for extract/mp4upload.py — stream URL resolver."""

import base64

import httpx
import pytest
import respx

from alt_ani_cli.extract.mp4upload import resolve

_EMBED = "https://www.mp4upload.com/embed-abc123.html"
_REFERER = "https://shinden.pl/"


def _mock_page(html: str):
    return respx.get(_EMBED).mock(return_value=httpx.Response(200, text=html))


@pytest.mark.unit
class TestResolveMp4upload:
    @respx.mock
    def test_player_src_pattern(self):
        _mock_page("<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>")
        stream = resolve(_EMBED, _REFERER)
        assert stream.url == "https://cdn.mp4upload.com/v/video.mp4"
        assert stream.ext == "mp4"

    @respx.mock
    def test_file_key_pattern(self):
        _mock_page('<script>var player = {file: "https://cdn.mp4upload.com/v/video.mp4"};</script>')
        stream = resolve(_EMBED, _REFERER)
        assert "mp4upload" in stream.url or stream.url.endswith(".mp4")

    @respx.mock
    def test_m3u8_ext_detection(self):
        _mock_page("<script>player.src('https://cdn.mp4upload.com/v/playlist.m3u8');</script>")
        stream = resolve(_EMBED, _REFERER)
        assert stream.ext == "m3u8"

    @respx.mock
    def test_base64_encoded_url(self):
        raw_url = "https://cdn.mp4upload.com/v/encoded_video.mp4"
        encoded = base64.b64encode(raw_url.encode()).decode()
        _mock_page(f'<script>var player = {{"file": "{encoded}"}};</script>')
        stream = resolve(_EMBED, _REFERER)
        assert stream.url == raw_url

    @respx.mock
    def test_raises_when_no_url_found(self):
        _mock_page("<html><body>no video here</body></html>")
        with pytest.raises(ValueError, match="mp4upload"):
            resolve(_EMBED, _REFERER)

    @respx.mock
    def test_referer_in_stream_headers(self):
        _mock_page("<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>")
        stream = resolve(_EMBED, _REFERER)
        assert stream.headers.get("Referer") == _EMBED
