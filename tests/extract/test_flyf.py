"""Tests for extract/flyf.py — single-step api.flyfile.app raw stream URL resolver."""

import pytest

from alt_ani_core.extract.flyf import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE, HttpStatusError

_EMBED = "https://flyf.lat/embed/Gdbv8BEkZhOFZ2O"
_REFERER = "https://shinden.pl/"

_ASSIGN_RESPONSE = {"url": "https://s1.flyfile.app", "token": "streamtok"}
_RAW_URL = "https://s1.flyfile.app/raw/streamtok"


@pytest.mark.unit
class TestResolveFlyf:
    def test_happy_path_resolves_to_raw(self, fake_session):
        session = fake_session(get=[_ASSIGN_RESPONSE])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == _RAW_URL
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers.get("Referer") == _EMBED
        assert stream.labels == ()

    def test_assign_failure_raises(self, fake_session):
        session = fake_session(get=[HttpStatusError(500)])
        with pytest.raises(HttpStatusError) as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.status == 500

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(get=[_ASSIGN_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_get_request_url_and_custom_headers(self, fake_session):
        session = fake_session(get=[_ASSIGN_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)

        session.get.assert_called_once()
        call = session.get.call_args
        assert call.args == ("https://api.flyfile.app/api/streaming/assign/Gdbv8BEkZhOFZ2O",)
        headers = call.kwargs["headers"]
        assert headers["Referer"] == _EMBED
        assert headers["X-FlyFile-View"] == "embed"
        assert headers["X-Embed-Referrer"] == _EMBED
        assert headers["X-Adblock-Detected"] == "0"

    def test_missing_url_or_token_raises_value_error(self, fake_session):
        session = fake_session(get=[{"url": "https://s1.flyfile.app"}])
        with pytest.raises(ValueError, match="flyf") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_non_dict_response_raises_value_error(self, fake_session):
        session = fake_session(get=[["unexpected"]])
        with pytest.raises(ValueError, match="flyf") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_bad_embed_url_raises_before_http(self, fake_session):
        session = fake_session(get=[_ASSIGN_RESPONSE])
        with pytest.raises(ValueError, match="flyf") as exc_info:
            resolve("not-a-url", _REFERER, session.transport)
        session.transport.open.assert_not_called()
        session.get.assert_not_called()
        assert exc_info.value.category == "parser_drift"


@pytest.mark.unit
class TestResolveFlyfFixture:
    _EMBED = "https://flyf.example/embed/testfilecode"

    def test_assign_fixture_resolves_to_raw_file(self, fake_session, load_fixture):
        session = fake_session(get=[load_fixture("flyf_assign.json")])
        stream = resolve(self._EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://cdn.example/raw/testtoken0000"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers["Referer"] == self._EMBED
        assert "User-Agent" in stream.default.headers

        call = session.get.call_args
        assert call.args == ("https://api.flyfile.app/api/streaming/assign/testfilecode",)
        headers = call.kwargs["headers"]
        assert headers["Referer"] == self._EMBED
        assert headers["X-FlyFile-View"] == "embed"
        assert headers["X-Embed-Referrer"] == self._EMBED
        assert headers["X-Adblock-Detected"] == "0"
