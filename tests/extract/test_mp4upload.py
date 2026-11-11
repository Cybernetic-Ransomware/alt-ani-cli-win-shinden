"""Tests for extract/mp4upload.py — stream URL resolver."""

import base64

import pytest
from alt_ani_core.extract.mp4upload import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

_EMBED = "https://www.mp4upload.com/embed-abc123.html"
_REFERER = "https://shinden.pl/"


@pytest.mark.unit
class TestResolveMp4upload:
    def test_player_src_pattern(self, fake_session):
        session = fake_session(get=["<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>"])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.mp4upload.com/v/video.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_file_key_pattern(self, fake_session):
        session = fake_session(get=['<script>var player = {file: "https://cdn.mp4upload.com/v/video.mp4"};</script>'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.mp4upload.com/v/video.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE

    def test_m3u8_ext_detection(self, fake_session):
        session = fake_session(get=["<script>player.src('https://cdn.mp4upload.com/v/playlist.m3u8');</script>"])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_base64_encoded_url(self, fake_session):
        raw_url = "https://cdn.mp4upload.com/v/encoded_video.mp4"
        encoded = base64.b64encode(raw_url.encode()).decode()
        session = fake_session(get=[f'<script>var player = {{"file": "{encoded}"}};</script>'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == raw_url
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_base64_extensionless_url_is_progressive_mp4(self, fake_session):
        raw_url = "https://cdn.mp4upload.com/d/testtoken0000/stream"
        encoded = base64.b64encode(raw_url.encode()).decode()
        session = fake_session(get=[f'<script>var player = {{"file": "{encoded}"}};</script>'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == raw_url
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_base64_m3u8_url_is_hls(self, fake_session):
        raw_url = "https://cdn.mp4upload.com/v/encoded/master.m3u8"
        encoded = base64.b64encode(raw_url.encode()).decode()
        session = fake_session(get=[f'<script>var player = {{"file": "{encoded}"}};</script>'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(get=["<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>"])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_raises_when_no_url_found(self, fake_session):
        session = fake_session(get=["<html><body>no video here</body></html>"])
        with pytest.raises(ValueError, match="mp4upload") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "parser_drift"

    def test_referer_in_stream_headers(self, fake_session):
        session = fake_session(get=["<script>player.src('https://cdn.mp4upload.com/v/video.mp4');</script>"])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.headers.get("Referer") == _EMBED


@pytest.mark.unit
class TestResolveMp4uploadFixture:
    def test_videojs_object_src_fixture_resolves(self, fake_session, load_fixture):
        session = fake_session(get=[load_fixture("mp4upload_embed.html")])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://cdn.example:183/d/testfilecodetesttoken/video.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers["Referer"] == _EMBED
        assert "User-Agent" in stream.default.headers

        call = session.get.call_args
        assert call.args == (_EMBED,)
        assert call.kwargs["headers"]["Referer"] == _REFERER
