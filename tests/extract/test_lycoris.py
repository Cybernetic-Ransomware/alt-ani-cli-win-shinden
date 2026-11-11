"""Tests for extract/lycoris.py - stream URL resolver."""

import pytest
from alt_ani_core.extract.lycoris import resolve
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

_EMBED = "https://www.lycoris.cafe/embed?id=180136&episode=1"
_REFERER = "https://shinden.pl/"

_API_RESPONSE = {
    "episodeInfo": {
        "primarySource": {
            "FHD": "https://od.lk/d/example/episode-1-1080p.mp4",
            "HD": "https://od.lk/d/example/episode-1-720p.mp4",
            "SD": "https://od.lk/d/example/episode-1-480p.mp4",
            "preview": "https://od.lk/d/example/preview.mp4",
            "SourceMKV": "https://od.lk/d/example/episode-1-1080p.mkv",
        }
    }
}


@pytest.mark.unit
class TestResolveLycoris:
    def test_happy_path_returns_best_url_and_qualities(self, fake_session):
        session = fake_session(get=[_API_RESPONSE])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://od.lk/d/example/episode-1-1080p.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        qualities = {v.label: v.url for v in stream.variants}
        assert stream.labels == ("1080p", "720p", "480p", "source-mkv")
        assert qualities["1080p"].endswith("1080p.mp4")
        assert qualities["720p"].endswith("720p.mp4")
        assert qualities["480p"].endswith("480p.mp4")
        assert qualities["source-mkv"].endswith("1080p.mkv")
        assert all(v.headers["Referer"] == _EMBED for v in stream.variants)
        assert stream.default.headers["Referer"] == _EMBED

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(get=[_API_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_every_variant_is_progressive_with_container_from_its_path(self, fake_session):
        session = fake_session(get=[_API_RESPONSE])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert all(v.protocol is StreamProtocol.PROGRESSIVE for v in stream.variants)
        assert {v.label: v.container for v in stream.variants} == {
            "1080p": "mp4",
            "720p": "mp4",
            "480p": "mp4",
            "source-mkv": "mkv",
        }

    def test_extensionless_url_is_progressive_mp4(self, fake_session):
        payload = {"episodeInfo": {"primarySource": {"HD": "https://od.lk/d/example/stream?name=episode.m3u8"}}}
        session = fake_session(get=[payload])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.labels == ("720p",)
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_source_mkv_alone_is_the_default(self, fake_session):
        payload = {"episodeInfo": {"primarySource": {"SourceMKV": "https://od.lk/d/example/episode-1.mkv"}}}
        session = fake_session(get=[payload])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.label == "source-mkv"
        assert stream.default.container == "mkv"

    def test_api_request_url_params_and_headers(self, fake_session):
        session = fake_session(get=[_API_RESPONSE])
        resolve(_EMBED, _REFERER, session.transport)

        call = session.get.call_args
        assert call.args == ("https://www.lycoris.cafe/api/embed",)
        assert call.kwargs["params"] == {"id": "180136", "episode": "1"}
        assert call.kwargs["headers"]["Referer"] == _EMBED

    def test_falls_back_to_hd_when_fhd_missing(self, fake_session):
        payload = {
            "episodeInfo": {
                "primarySource": {
                    "HD": "https://od.lk/d/example/episode-1-720p.mp4",
                    "SD": "https://od.lk/d/example/episode-1-480p.mp4",
                }
            }
        }
        session = fake_session(get=[payload])
        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url.endswith("720p.mp4")
        assert {v.label: v.url for v in stream.variants} == {
            "720p": "https://od.lk/d/example/episode-1-720p.mp4",
            "480p": "https://od.lk/d/example/episode-1-480p.mp4",
        }

    def test_missing_primary_source_raises_value_error(self, fake_session):
        session = fake_session(get=[{"episodeInfo": {}}])
        with pytest.raises(ValueError, match="lycoris") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_missing_stream_urls_raises_value_error(self, fake_session):
        session = fake_session(get=[{"episodeInfo": {"primarySource": {}}}])
        with pytest.raises(ValueError, match="lycoris") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "no_stream_url"

    def test_bad_embed_url_raises_before_http(self, fake_session):
        session = fake_session(get=[_API_RESPONSE])
        with pytest.raises(ValueError, match="lycoris") as exc_info:
            resolve("not-a-url", _REFERER, session.transport)
        session.transport.open.assert_not_called()
        session.get.assert_not_called()
        assert exc_info.value.category == "parser_drift"


@pytest.mark.unit
class TestResolveLycorisFixture:
    _EMBED = "https://lycoris.example/embed?id=4242&episode=1"

    def test_embed_api_fixture_resolves_best_url_and_qualities(self, fake_session, load_fixture):
        session = fake_session(get=[load_fixture("lycoris_embed.json")])
        stream = resolve(self._EMBED, _REFERER, session.transport)

        base = "https://cdn.example/d/testfilecode"
        assert stream.default.url == f"{base}/episode-1-1080p.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert {v.label: v.url for v in stream.variants} == {
            "1080p": f"{base}/episode-1-1080p.mp4",
            "720p": f"{base}/episode-1-720p.mp4",
            "480p": f"{base}/episode-1-480p.mp4",
            "source-mkv": f"{base}/episode-1-1080p.mkv",
        }
        assert stream.default.headers["Referer"] == self._EMBED
        assert all(v.protocol is StreamProtocol.PROGRESSIVE for v in stream.variants)
        assert stream.select("source-mkv").container == "mkv"

        call = session.get.call_args
        assert call.args == ("https://lycoris.example/api/embed",)
        assert call.kwargs["params"] == {"id": "4242", "episode": "1"}
        assert call.kwargs["headers"]["Referer"] == self._EMBED
