"""Tests for extract/playmate.py — stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.playmate import resolve

_EMBED = "https://playmate.to/embed/MFyuwmxvBGiUE"
_REFERER = "https://shinden.pl/"

_M3U8_URL = "https://cdn.playmate.to/hls/abc/master.m3u8?token=deadbeef"

_API_RESPONSE = {"sx": _M3U8_URL}


def _make_session_patch(payload):
    resp = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    resp.json.return_value = payload
    session = MagicMock()
    session.post.return_value = resp
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.playmate.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolvePlaymate:
    def test_happy_path_url_headers_and_ext(self):
        with _make_session_patch(_API_RESPONSE):
            stream = resolve(_EMBED, _REFERER)

        assert stream.url == _M3U8_URL
        assert stream.headers.get("Referer") == _EMBED
        assert stream.headers.get("Origin") == "https://playmate.to"
        assert stream.ext == "m3u8"

    def test_api_request_url_payload_and_headers(self):
        with _make_session_patch(_API_RESPONSE) as session:
            resolve(_EMBED, _REFERER)

        call = session.post.call_args
        assert call.args == ("https://playmate.to/api/s",)
        assert call.kwargs["json"] == {"c": "MFyuwmxvBGiUE", "d": "web"}
        assert call.kwargs["headers"]["Origin"] == "https://playmate.to"
        assert call.kwargs["headers"]["Referer"] == _EMBED

    def test_missing_sx_raises_value_error(self):
        with _make_session_patch({"status": 404}):
            with pytest.raises(ValueError, match="playmate"):
                resolve(_EMBED, _REFERER)

    def test_non_dict_response_raises_value_error(self):
        with _make_session_patch(["unexpected"]):
            with pytest.raises(ValueError, match="playmate"):
                resolve(_EMBED, _REFERER)

    def test_bad_embed_url_raises_before_http(self):
        with _make_session_patch(_API_RESPONSE) as session:
            with pytest.raises(ValueError, match="playmate"):
                resolve("not-a-url", _REFERER)
        session.post.assert_not_called()

    def test_mp4_sx_gets_mp4_ext(self):
        with _make_session_patch({"sx": "https://cdn.example.com/video.mp4"}):
            stream = resolve(_EMBED, _REFERER)
        assert stream.ext == "mp4"
