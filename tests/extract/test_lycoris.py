"""Tests for extract/lycoris.py - stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.lycoris import resolve

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


def _make_session_patch(payload):
    resp = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    resp.json.return_value = payload
    session = MagicMock()
    session.get.return_value = resp
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.lycoris.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolveLycoris:
    def test_happy_path_returns_best_url_and_qualities(self):
        with _make_session_patch(_API_RESPONSE):
            stream = resolve(_EMBED, _REFERER)

        assert stream.url == "https://od.lk/d/example/episode-1-1080p.mp4"
        assert stream.ext == "mp4"
        assert stream.qualities["1080p"].endswith("1080p.mp4")
        assert stream.qualities["720p"].endswith("720p.mp4")
        assert stream.qualities["480p"].endswith("480p.mp4")
        assert stream.qualities["source-mkv"].endswith("1080p.mkv")
        assert stream.headers["Referer"] == _EMBED

    def test_api_request_url_params_and_headers(self):
        with _make_session_patch(_API_RESPONSE) as session:
            resolve(_EMBED, _REFERER)

        call = session.get.call_args
        assert call.args == ("https://www.lycoris.cafe/api/embed",)
        assert call.kwargs["params"] == {"id": "180136", "episode": "1"}
        assert call.kwargs["headers"]["Referer"] == _EMBED

    def test_falls_back_to_hd_when_fhd_missing(self):
        payload = {
            "episodeInfo": {
                "primarySource": {
                    "HD": "https://od.lk/d/example/episode-1-720p.mp4",
                    "SD": "https://od.lk/d/example/episode-1-480p.mp4",
                }
            }
        }
        with _make_session_patch(payload):
            stream = resolve(_EMBED, _REFERER)

        assert stream.url.endswith("720p.mp4")
        assert stream.qualities == {
            "720p": "https://od.lk/d/example/episode-1-720p.mp4",
            "480p": "https://od.lk/d/example/episode-1-480p.mp4",
        }

    def test_missing_primary_source_raises_value_error(self):
        with _make_session_patch({"episodeInfo": {}}):
            with pytest.raises(ValueError, match="lycoris"):
                resolve(_EMBED, _REFERER)

    def test_missing_stream_urls_raises_value_error(self):
        with _make_session_patch({"episodeInfo": {"primarySource": {}}}):
            with pytest.raises(ValueError, match="lycoris"):
                resolve(_EMBED, _REFERER)

    def test_bad_embed_url_raises_before_http(self):
        with _make_session_patch(_API_RESPONSE) as session:
            with pytest.raises(ValueError, match="lycoris"):
                resolve("not-a-url", _REFERER)
        session.get.assert_not_called()
