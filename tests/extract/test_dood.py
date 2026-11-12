"""Tests for extract/dood.py — stream URL resolver."""

from unittest.mock import call, patch

import pytest

from alt_ani_core.extract.dood import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

_EMBED = "https://dood.la/e/xyz"
_REFERER = "https://shinden.pl/"

_PAGE_WITH_TOKEN = """
<script>
$.get('/pass_md5/12/abc-DEF_9', function(data) { makePlay(data); });
makePlay('?token=tok123abc');
</script>
"""

_PAGE_NO_TOKEN = """
<script>
$.get('/pass_md5/12/abc-DEF_9', function(data) { makePlay(data); });
</script>
"""

_PAGE_NO_PASS_MD5 = "<html><body>no pass_md5 here</body></html>"

_PASS_MD5_RESPONSE = "https://d1.dood.video/seg"


@pytest.mark.unit
class TestResolveDood:
    def test_happy_path_url_and_headers(self, fake_session):
        session = fake_session(get=[_PAGE_WITH_TOKEN, _PASS_MD5_RESPONSE])
        with patch("alt_ani_core.extract.dood.time.time", return_value=1000.0):
            stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://d1.dood.video/seg?token=tok123abc&expiry=1000000"
        assert stream.default.headers.get("Referer") == "https://dood.la/"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.labels == ()

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(get=[_PAGE_WITH_TOKEN, _PASS_MD5_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_happy_path_second_request_url_and_headers(self, fake_session):
        session = fake_session(get=[_PAGE_WITH_TOKEN, _PASS_MD5_RESPONSE])
        with patch("alt_ani_core.extract.dood.time.time", return_value=1000.0):
            resolve(_EMBED, _REFERER, session.transport)

        second_call = session.get.call_args_list[1]
        assert second_call == call(
            "https://dood.la/pass_md5/12/abc-DEF_9",
            headers={
                "Referer": _EMBED,
                "User-Agent": second_call.kwargs["headers"]["User-Agent"],
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def test_no_token_in_page_url_has_no_query(self, fake_session):
        session = fake_session(get=[_PAGE_NO_TOKEN, _PASS_MD5_RESPONSE])
        with patch("alt_ani_core.extract.dood.time.time", return_value=1000.0):
            stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == _PASS_MD5_RESPONSE

    def test_extensionless_url_is_progressive_mp4(self, fake_session):
        session = fake_session(get=[_PAGE_NO_TOKEN, _PASS_MD5_RESPONSE])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_no_pass_md5_raises_value_error_after_one_request(self, fake_session):
        session = fake_session(get=[_PAGE_NO_PASS_MD5])
        with pytest.raises(ValueError, match="dood") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert session.get.call_count == 1
        assert exc_info.value.category == "parser_drift"

    def test_bad_base_url_raises_before_http(self, fake_session):
        session = fake_session()
        with pytest.raises(ValueError, match="dood") as exc_info:
            resolve("not-a-url", _REFERER, session.transport)
        session.transport.open.assert_not_called()
        session.get.assert_not_called()
        assert exc_info.value.category == "parser_drift"


@pytest.mark.unit
class TestResolveDoodFixture:
    _EMBED = "https://dood.example/e/testfilecode"

    def test_embed_and_pass_md5_fixtures_resolve_to_tokenized_url(self, fake_session, load_fixture):
        session = fake_session(get=[load_fixture("dood_embed.html"), load_fixture("dood_pass_md5.txt")])
        with patch("alt_ani_core.extract.dood.time.time", return_value=1700000000.0):
            stream = resolve(self._EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://cdn.example/stream/testfilecode~?token=testtokenAbc123&expiry=1700000000000"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers["Referer"] == "https://dood.example/"
        assert "User-Agent" in stream.default.headers

        embed_call, pass_call = session.get.call_args_list
        assert embed_call.args == (self._EMBED,)
        assert embed_call.kwargs["headers"]["Referer"] == _REFERER
        assert pass_call.args == ("https://dood.example/pass_md5/42-1700000000-testhash/testfilecode",)
        assert pass_call.kwargs["headers"]["Referer"] == self._EMBED
        assert pass_call.kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
