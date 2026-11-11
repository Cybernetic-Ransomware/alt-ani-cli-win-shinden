"""Tests for extract/uqload.py — stream URL resolver."""

import pytest
from alt_ani_core.extract.uqload import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

_EMBED = "https://uqload.is/e/mhq7xbccftp5"
_REFERER = "https://shinden.pl/"


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
    def test_happy_path_unpacked_body(self, fake_session):
        session = fake_session(post=[_UNPACKED_BODY])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.uqload.is/video.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers.get("Referer") == _EMBED

    def test_base_62_packed_body_decodes_end_to_end(self, fake_session):
        session = fake_session(post=[_PACKED_BODY])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.uqload.is/packed.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(post=[_UNPACKED_BODY])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_extensionless_url_is_progressive_mp4(self, fake_session):
        session = fake_session(post=['<script>var player = {file: "https://cdn.uqload.is/d/abc?src=list.m3u8"};</script>'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.uqload.is/d/abc?src=list.m3u8"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_dl_request_form_payload_and_headers(self, fake_session):
        session = fake_session(post=[_UNPACKED_BODY])
        resolve(_EMBED, _REFERER, session.transport)

        call = session.post.call_args
        assert call.args == ("https://uqload.is/dl",)
        assert call.kwargs["data"] == {
            "op": "embed",
            "file_code": "mhq7xbccftp5",
            "auto": "0",
            "referer": _EMBED,
        }
        assert call.kwargs["headers"]["Referer"] == _EMBED

    def test_no_match_raises_value_error(self, fake_session):
        session = fake_session(post=[_NO_MATCH])
        with pytest.raises(ValueError, match="uqload") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "parser_drift"

    def test_bad_embed_url_raises_before_http(self, fake_session):
        session = fake_session(post=[_UNPACKED_BODY])
        with pytest.raises(ValueError, match="uqload") as exc_info:
            resolve("not-a-url", _REFERER, session.transport)
        session.transport.open.assert_not_called()
        session.post.assert_not_called()
        assert exc_info.value.category == "parser_drift"

    def test_m3u8_url_gets_m3u8_ext(self, fake_session):
        html = '<script>var player = {file: "https://cdn.uqload.is/video.m3u8"};</script>'
        session = fake_session(post=[html])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None


@pytest.mark.unit
class TestResolveUqloadFixture:
    def test_packed_dl_fixture_resolves_after_unpacking(self, fake_session, load_fixture):
        body = load_fixture("uqload_dl_packed.html")
        assert "v.mp4" not in body
        session = fake_session(post=[body])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://cdn.example/3rfktestfilecode/v.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers["Referer"] == _EMBED

        call = session.post.call_args
        assert call.args == ("https://uqload.is/dl",)
        assert call.kwargs["data"] == {"op": "embed", "file_code": "mhq7xbccftp5", "auto": "0", "referer": _EMBED}
        assert call.kwargs["headers"]["Referer"] == _EMBED
