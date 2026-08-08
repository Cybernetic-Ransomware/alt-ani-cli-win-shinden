"""Tests for extract/flyf.py — two-step api.flyfile.app stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.flyf import resolve

_EMBED = "https://flyf.lat/embed/Gdbv8BEkZhOFZ2O"
_REFERER = "https://shinden.pl/"

_ASSIGN_RESPONSE = {"url": "https://s1.flyfile.app", "token": "streamtok"}

_HLS_URL = "https://s1.flyfile.app/hls/streamtok/master.m3u8"
_RAW_URL = "https://s1.flyfile.app/raw/streamtok"

_METADATA_HLS_READY = {"videoAsset": {"qualities": [{"label": "720p", "status": "READY"}]}}
_METADATA_HLS_PROCESSING = {"videoAsset": {"qualities": [{"label": "720p", "status": "PROCESSING"}]}}


def _make_session_patch(step1, step2_json, step1_raises=False):
    session = MagicMock()

    if step1_raises:
        resp1 = MagicMock(status_code=500)
        resp1.raise_for_status.side_effect = Exception("upstream error")
    else:
        resp1 = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
        resp1.json.return_value = step1

    resp2 = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    resp2.json.return_value = step2_json

    session.get.side_effect = [resp1, resp2]
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.flyf.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolveFlyf:
    def test_happy_path_hls_ready_from_metadata(self):
        with _make_session_patch(_METADATA_HLS_READY, _ASSIGN_RESPONSE):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _HLS_URL
        assert stream.ext == "m3u8"
        assert stream.headers.get("Referer") == _EMBED

    def test_hls_not_ready_falls_back_to_raw(self):
        with _make_session_patch(_METADATA_HLS_PROCESSING, _ASSIGN_RESPONSE):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _RAW_URL
        assert stream.ext == "mp4"

    def test_missing_metadata_defaults_to_raw(self):
        with _make_session_patch({}, _ASSIGN_RESPONSE):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _RAW_URL
        assert stream.ext == "mp4"

    def test_non_dict_metadata_defaults_to_raw(self):
        with _make_session_patch(["unexpected"], _ASSIGN_RESPONSE):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _RAW_URL

    def test_non_list_qualities_defaults_to_raw(self):
        with _make_session_patch({"videoAsset": {"qualities": "not-a-list"}}, _ASSIGN_RESPONSE):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _RAW_URL

    def test_step1_failure_is_non_fatal_falls_back_to_raw(self):
        with _make_session_patch(None, _ASSIGN_RESPONSE, step1_raises=True):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _RAW_URL
        assert stream.ext == "mp4"

    def test_step2_failure_raises_value_error(self):
        session = MagicMock()
        resp1 = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
        resp1.json.return_value = _METADATA_HLS_READY
        resp2 = MagicMock(status_code=500)
        resp2.raise_for_status.side_effect = Exception("upstream error")
        session.get.side_effect = [resp1, resp2]
        session.__enter__ = lambda s: session
        session.__exit__ = MagicMock(return_value=False)

        with patch("alt_ani_cli.extract.flyf.cffi_requests.Session", return_value=session):
            with pytest.raises(Exception, match="upstream error"):
                resolve(_EMBED, _REFERER)

    def test_get_request_urls_and_custom_headers(self):
        with _make_session_patch(_METADATA_HLS_READY, _ASSIGN_RESPONSE) as session:
            resolve(_EMBED, _REFERER)

        first_call, second_call = session.get.call_args_list
        assert first_call.args == ("https://api.flyfile.app/api/public/file/Gdbv8BEkZhOFZ2O",)
        assert second_call.args == ("https://api.flyfile.app/api/streaming/assign/Gdbv8BEkZhOFZ2O",)
        for call in (first_call, second_call):
            headers = call.kwargs["headers"]
            assert headers["Referer"] == _EMBED
            assert headers["X-FlyFile-View"] == "embed"
            assert headers["X-Embed-Referrer"] == _EMBED
            assert headers["X-Adblock-Detected"] == "0"

    def test_missing_url_or_token_raises_value_error(self):
        with _make_session_patch(_METADATA_HLS_READY, {"url": "https://s1.flyfile.app"}):
            with pytest.raises(ValueError, match="flyf"):
                resolve(_EMBED, _REFERER)

    def test_bad_embed_url_raises_before_http(self):
        with _make_session_patch(_METADATA_HLS_READY, _ASSIGN_RESPONSE) as session:
            with pytest.raises(ValueError, match="flyf"):
                resolve("not-a-url", _REFERER)
        session.get.assert_not_called()
