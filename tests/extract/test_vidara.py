"""Tests for extract/vidara.py — stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.vidara import resolve

_EMBED = "https://vidara.to/e/gGnwW3ekLDWQX"
_REFERER = "https://shinden.pl/"

_HLS_URL = "https://s25-wyl4.97bf1.com/hls/abc/master.m3u8?token=deadbeef-1783876903"

_API_RESPONSE = {
    "default_sub_lang": "Polish",
    "filecode": "gGnwW3ekLDWQX",
    "streaming_url": _HLS_URL,
    "subtitles": None,
    "title": "",
}


def _make_session_patch(payload):
    resp = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    resp.json.return_value = payload
    session = MagicMock()
    session.post.return_value = resp
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.vidara.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolveVidara:
    def test_happy_path_url_headers_and_ext(self):
        with _make_session_patch(_API_RESPONSE):
            stream = resolve(_EMBED, _REFERER)

        assert stream.url == _HLS_URL
        assert stream.headers.get("Referer") == "https://vidara.to/"
        assert stream.headers.get("Origin") == "https://vidara.to"
        assert stream.ext == "m3u8"

    def test_api_request_url_payload_and_headers(self):
        with _make_session_patch(_API_RESPONSE) as session:
            resolve(_EMBED, _REFERER)

        call = session.post.call_args
        assert call.args == ("https://vidara.to/api/stream",)
        assert call.kwargs["json"] == {"filecode": "gGnwW3ekLDWQX", "device": "web"}
        assert call.kwargs["headers"]["Origin"] == "https://vidara.to"
        assert call.kwargs["headers"]["Referer"] == _EMBED

    def test_missing_streaming_url_raises_value_error(self):
        with _make_session_patch({"status": 404}):
            with pytest.raises(ValueError, match="vidara"):
                resolve(_EMBED, _REFERER)

    def test_non_dict_response_raises_value_error(self):
        with _make_session_patch(["unexpected"]):
            with pytest.raises(ValueError, match="vidara"):
                resolve(_EMBED, _REFERER)

    def test_bad_embed_url_raises_before_http(self):
        with _make_session_patch(_API_RESPONSE) as session:
            with pytest.raises(ValueError, match="vidara"):
                resolve("not-a-url", _REFERER)
        session.post.assert_not_called()

    def test_mp4_streaming_url_gets_mp4_ext(self):
        with _make_session_patch({**_API_RESPONSE, "streaming_url": "https://cdn.example.com/video.mp4"}):
            stream = resolve(_EMBED, _REFERER)
        assert stream.ext == "mp4"
