"""Tests for extract/playmate.py — stream URL resolver."""

import pytest
from alt_ani_core.extract.playmate import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

_EMBED = "https://playmate.to/embed/MFyuwmxvBGiUE"
_REFERER = "https://shinden.pl/"

_M3U8_URL = "https://cdn.playmate.to/hls/abc/master.txt?token=deadbeef"

_API_RESPONSE = {"sx": _M3U8_URL}


@pytest.mark.unit
class TestResolvePlaymate:
    def test_happy_path_url_headers_and_ext(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == _M3U8_URL
        assert stream.default.headers.get("Referer") == _EMBED
        assert stream.default.headers.get("Origin") == "https://playmate.to"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_api_request_url_payload_and_headers(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)

        call = session.post.call_args
        assert call.args == ("https://playmate.to/api/s",)
        assert call.kwargs["json"] == {"c": "MFyuwmxvBGiUE", "d": "web"}
        assert call.kwargs["headers"]["Origin"] == "https://playmate.to"
        assert call.kwargs["headers"]["Referer"] == _EMBED
        assert call.kwargs["headers"]["Sec-Fetch-Site"] == "same-origin"

    def test_missing_sx_raises_value_error(self, fake_session):
        session = fake_session(post=[{"status": 404}])
        with pytest.raises(ValueError, match="playmate") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_non_dict_response_raises_value_error(self, fake_session):
        session = fake_session(post=[["unexpected"]])
        with pytest.raises(ValueError, match="playmate") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_bad_embed_url_raises_before_http(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        with pytest.raises(ValueError, match="playmate") as exc_info:
            resolve("not-a-url", _REFERER, session.transport)
        session.transport.open.assert_not_called()
        session.post.assert_not_called()
        assert exc_info.value.category == "parser_drift"

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_extensionless_sx_is_progressive_mp4(self, fake_session):
        session = fake_session(post=[{"sx": "https://cdn.example.com/v/abc?name=master.m3u8"}])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_m3u8_sx_is_hls(self, fake_session):
        session = fake_session(post=[{"sx": "https://cdn.example.com/v/abc/master.m3u8"}])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_mp4_sx_gets_mp4_ext(self, fake_session):
        session = fake_session(post=[{"sx": "https://cdn.example.com/video.mp4"}])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"


@pytest.mark.unit
class TestResolvePlaymateFixture:
    _EMBED = "https://playmate.example/embed/testfilecode"

    def test_api_fixture_master_txt_is_recognised_as_hls(self, fake_session, load_fixture):
        session = fake_session(post=[load_fixture("playmate_stream.json")])
        stream = resolve(self._EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://cdn.example/hls/testfilecode/master.txt?t=testtoken&e=1700000000"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None
        assert stream.default.headers["Referer"] == self._EMBED
        assert stream.default.headers["Origin"] == "https://playmate.example"

        call = session.post.call_args
        assert call.args == ("https://playmate.example/api/s",)
        assert call.kwargs["json"] == {"c": "testfilecode", "d": "web"}
        headers = call.kwargs["headers"]
        assert headers["Origin"] == "https://playmate.example"
        assert headers["Referer"] == self._EMBED
        assert headers["Sec-Fetch-Site"] == "same-origin"
