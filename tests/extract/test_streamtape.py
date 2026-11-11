"""Tests for extract/streamtape.py — stream URL resolver."""

import pytest
from alt_ani_core.extract.streamtape import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

_EMBED = "https://streamtape.com/e/abc123"
_REFERER = "https://shinden.pl/"


_ROBOTLINK_PROTO_REL = """
<script>
var robotlink = document.getElementById('robotlink');
robotlink.innerHTML = '//streamtape.com/get_video?id=abc' + '&expires=1&ip=x&token=tok';
</script>
"""

_ROBOTLINK_RELATIVE = """
<script>
var robotlink = document.getElementById('robotlink');
robotlink.innerHTML = '/get_video?id=a' + '&t=b';
</script>
"""

_DIRECT_URL = """
<script>
var url = "https://streamtape.com/get_video?id=zz&token=tt";
</script>
"""

_NO_MATCH = "<html><body>no video here</body></html>"


@pytest.mark.unit
class TestResolveStreamtape:
    def test_robotlink_protocol_relative_prefixed_with_https(self, fake_session):
        session = fake_session(get=[_ROBOTLINK_PROTO_REL])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://streamtape.com/get_video?id=abc&expires=1&ip=x&token=tok"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers.get("Referer") == _EMBED

    def test_robotlink_relative_path_prefixed_with_host(self, fake_session):
        session = fake_session(get=[_ROBOTLINK_RELATIVE])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://streamtape.com/get_video?id=a&t=b"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(get=[_ROBOTLINK_RELATIVE])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_direct_url_fallback(self, fake_session):
        session = fake_session(get=[_DIRECT_URL])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://streamtape.com/get_video?id=zz&token=tt"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers.get("Referer") == _EMBED

    def test_no_match_raises_value_error(self, fake_session):
        session = fake_session(get=[_NO_MATCH])
        with pytest.raises(ValueError, match="streamtape") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "parser_drift"


@pytest.mark.unit
class TestResolveStreamtapeFixture:
    def test_robotlink_concatenation_fixture_ignores_decoy_div(self, fake_session, load_fixture):
        session = fake_session(get=[load_fixture("streamtape_embed.html")])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == (
            "https://streamtape.example/get_video?id=testfilecode&expires=1700000000&ip=TESTIP0000&token=testtoken0000"
        )
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers["Referer"] == _EMBED

        call = session.get.call_args
        assert call.args == (_EMBED,)
        assert call.kwargs["headers"]["Referer"] == _REFERER
