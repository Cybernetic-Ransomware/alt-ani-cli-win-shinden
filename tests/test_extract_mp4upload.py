import base64
import pytest
import respx
import httpx

from alt_ani_cli.extract.mp4upload import resolve

_EMBED = "https://www.mp4upload.com/embed-abc123.html"
_REFERER = "https://shinden.pl/"


def _mock_page(html: str):
    return respx.get(_EMBED).mock(return_value=httpx.Response(200, text=html))


@respx.mock
def test_player_src_pattern():
    html = "<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>"
    _mock_page(html)
    stream = resolve(_EMBED, _REFERER)
    assert stream.url == "https://cdn.mp4upload.com/v/video.mp4"
    assert stream.ext == "mp4"


@respx.mock
def test_file_key_pattern():
    html = '<script>var player = {file: "https://cdn.mp4upload.com/v/video.mp4"};</script>'
    _mock_page(html)
    stream = resolve(_EMBED, _REFERER)
    assert "mp4upload" in stream.url or stream.url.endswith(".mp4")


@respx.mock
def test_m3u8_ext_detection():
    html = "<script>player.src('https://cdn.mp4upload.com/v/playlist.m3u8');</script>"
    _mock_page(html)
    stream = resolve(_EMBED, _REFERER)
    assert stream.ext == "m3u8"


@respx.mock
def test_base64_encoded_url():
    raw_url = "https://cdn.mp4upload.com/v/encoded_video.mp4"
    encoded = base64.b64encode(raw_url.encode()).decode()
    html = f'<script>var player = {{"file": "{encoded}"}};</script>'
    _mock_page(html)
    stream = resolve(_EMBED, _REFERER)
    assert stream.url == raw_url


@respx.mock
def test_raises_when_no_url_found():
    _mock_page("<html><body>no video here</body></html>")
    with pytest.raises(ValueError, match="mp4upload"):
        resolve(_EMBED, _REFERER)


@respx.mock
def test_referer_in_stream_headers():
    html = "<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>"
    _mock_page(html)
    stream = resolve(_EMBED, _REFERER)
    assert stream.headers.get("Referer") == _EMBED
