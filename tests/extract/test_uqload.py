"""Tests for extract/uqload.py — stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.uqload import resolve

_EMBED = "https://uqload.is/e/mhq7xbccftp5"
_REFERER = "https://shinden.pl/"


def _make_session_patch(html: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    resp.raise_for_status.return_value = None

    session = MagicMock()
    session.post.return_value = resp
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.uqload.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


_UNPACKED_BODY = '<script>var player = {file: "https://cdn.uqload.is/video.mp4"};</script>'

_NO_MATCH = "<html><body>no video here</body></html>"

_PACKED_KEYS = [*(f"k{i}" for i in range(36)), "file", "https://cdn.uqload.is/packed.mp4"]
_PACKED_BODY = (
    "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
    f"('A:\"B\"',62,{len(_PACKED_KEYS)},'{'|'.join(_PACKED_KEYS)}'.split('|')))"
    "</script>"
)


@pytest.mark.unit
class TestResolveUqload:
    def test_happy_path_unpacked_body(self):
        with _make_session_patch(_UNPACKED_BODY):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == "https://cdn.uqload.is/video.mp4"
        assert stream.ext == "mp4"
        assert stream.headers.get("Referer") == _EMBED

    def test_base_62_packed_body_decodes_end_to_end(self):
        with _make_session_patch(_PACKED_BODY):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == "https://cdn.uqload.is/packed.mp4"

    def test_dl_request_form_payload_and_headers(self):
        with _make_session_patch(_UNPACKED_BODY) as session:
            resolve(_EMBED, _REFERER)

        call = session.post.call_args
        assert call.args == ("https://uqload.is/dl",)
        assert call.kwargs["data"] == {
            "op": "embed",
            "file_code": "mhq7xbccftp5",
            "auto": "0",
            "referer": _EMBED,
        }
        assert call.kwargs["headers"]["Referer"] == _EMBED

    def test_no_match_raises_value_error(self):
        with _make_session_patch(_NO_MATCH):
            with pytest.raises(ValueError, match="uqload"):
                resolve(_EMBED, _REFERER)

    def test_bad_embed_url_raises_before_http(self):
        with _make_session_patch(_UNPACKED_BODY) as session:
            with pytest.raises(ValueError, match="uqload"):
                resolve("not-a-url", _REFERER)
        session.post.assert_not_called()

    def test_m3u8_url_gets_m3u8_ext(self):
        html = '<script>var player = {file: "https://cdn.uqload.is/video.m3u8"};</script>'
        with _make_session_patch(html):
            stream = resolve(_EMBED, _REFERER)
        assert stream.ext == "m3u8"
