"""Tests for extract/vidara.py — stream URL resolver."""

import pytest

from alt_ani_core.extract.vidara import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

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


@pytest.mark.unit
class TestResolveVidara:
    def test_happy_path_url_headers_and_ext(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == _HLS_URL
        assert stream.default.headers.get("Referer") == _EMBED
        assert stream.default.headers.get("Origin") == "https://vidara.to"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_api_request_url_payload_and_headers(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)

        call = session.post.call_args
        assert call.args == ("https://vidara.to/api/stream",)
        assert call.kwargs["json"] == {"filecode": "gGnwW3ekLDWQX", "device": "web"}
        assert call.kwargs["headers"]["Origin"] == "https://vidara.to"
        assert call.kwargs["headers"]["Referer"] == _EMBED

    def test_missing_streaming_url_raises_value_error(self, fake_session):
        session = fake_session(post=[{"status": 404}])
        with pytest.raises(ValueError, match="vidara") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_non_dict_response_raises_value_error(self, fake_session):
        session = fake_session(post=[["unexpected"]])
        with pytest.raises(ValueError, match="vidara") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_bad_embed_url_raises_before_http(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        with pytest.raises(ValueError, match="vidara") as exc_info:
            resolve("not-a-url", _REFERER, session.transport)
        session.transport.open.assert_not_called()
        session.post.assert_not_called()
        assert exc_info.value.category == "parser_drift"

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(post=[_API_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_extensionless_streaming_url_is_progressive_mp4(self, fake_session):
        session = fake_session(post=[{**_API_RESPONSE, "streaming_url": "https://cdn.example.com/v/abc?name=master.m3u8"}])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_hls_directory_without_m3u8_suffix_is_hls(self, fake_session):
        session = fake_session(post=[{**_API_RESPONSE, "streaming_url": "https://cdn.example.com/hls/abc/master.txt"}])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_mp4_streaming_url_gets_mp4_ext(self, fake_session):
        session = fake_session(post=[{**_API_RESPONSE, "streaming_url": "https://cdn.example.com/video.mp4"}])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_works_with_alternate_host_viewdara(self, fake_session):
        embed = "https://viewdara.com/e/gGnwW3ekLDWQX"
        session = fake_session(post=[_API_RESPONSE])
        resolve(embed, _REFERER, session.transport)

        call = session.post.call_args
        assert call.args == ("https://viewdara.com/api/stream",)
        assert call.kwargs["headers"]["Origin"] == "https://viewdara.com"


@pytest.mark.unit
class TestResolveVidaraFixture:
    @pytest.mark.parametrize("host", ["vidara.to", "iosbgaigo.com"])
    def test_stream_api_fixture_is_host_agnostic(self, fake_session, load_fixture, host):
        embed = f"https://{host}/e/testfilecode"
        session = fake_session(post=[load_fixture("vidara_stream.json")])
        stream = resolve(embed, _REFERER, session.transport)

        assert stream.default.url == "https://cdn.example/hls/testfilecode/master.m3u8?t=testtoken&e=1700000000"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None
        assert stream.default.headers["Referer"] == embed
        assert stream.default.headers["Origin"] == f"https://{host}"

        call = session.post.call_args
        assert call.args == (f"https://{host}/api/stream",)
        assert call.kwargs["json"] == {"filecode": "testfilecode", "device": "web"}
        assert call.kwargs["headers"]["Origin"] == f"https://{host}"
        assert call.kwargs["headers"]["Referer"] == embed
