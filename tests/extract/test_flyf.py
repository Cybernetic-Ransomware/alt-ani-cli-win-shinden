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


def _make_session_patch(step2_json, head_statuses):
    resp1 = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    resp2 = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    resp2.json.return_value = step2_json

    session = MagicMock()
    session.get.side_effect = [resp1, resp2]
    session.head.side_effect = [MagicMock(status_code=s) for s in head_statuses]
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.flyf.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolveFlyf:
    def test_happy_path_hls_probe_succeeds(self):
        with _make_session_patch(_ASSIGN_RESPONSE, [200]):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _HLS_URL
        assert stream.ext == "m3u8"
        assert stream.headers.get("Referer") == _EMBED

    def test_hls_probe_fails_falls_back_to_raw(self):
        with _make_session_patch(_ASSIGN_RESPONSE, [404, 200]):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _RAW_URL
        assert stream.ext == "mp4"

    def test_both_probes_fail_raises_value_error(self):
        with _make_session_patch(_ASSIGN_RESPONSE, [404, 404]):
            with pytest.raises(ValueError, match="flyf"):
                resolve(_EMBED, _REFERER)

    def test_get_request_urls_and_custom_headers(self):
        with _make_session_patch(_ASSIGN_RESPONSE, [200]) as session:
            resolve(_EMBED, _REFERER)

        first_call, second_call = session.get.call_args_list
        assert first_call.args == ("https://api.flyfile.app/api/public/file/Gdbv8BEkZhOFZ2O",)
        assert second_call.args == ("https://api.flyfile.app/api/streaming/assign/Gdbv8BEkZhOFZ2O",)
        for call in (first_call, second_call):
            headers = call.kwargs["headers"]
            assert headers["Referer"] == _EMBED
            assert headers["X-FlyFile-View"] == "1"
            assert headers["X-Embed-Referrer"] == _REFERER
            assert headers["X-Adblock-Detected"] == "false"

    def test_missing_url_or_token_raises_before_any_probe(self):
        with _make_session_patch({"url": "https://s1.flyfile.app"}, []) as session:
            with pytest.raises(ValueError, match="flyf"):
                resolve(_EMBED, _REFERER)
        session.head.assert_not_called()

    def test_bad_embed_url_raises_before_http(self):
        with _make_session_patch(_ASSIGN_RESPONSE, [200]) as session:
            with pytest.raises(ValueError, match="flyf"):
                resolve("not-a-url", _REFERER)
        session.get.assert_not_called()
