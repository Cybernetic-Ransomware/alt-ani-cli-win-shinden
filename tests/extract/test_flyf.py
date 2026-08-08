"""Tests for extract/flyf.py — single-step api.flyfile.app raw stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.flyf import resolve

_EMBED = "https://flyf.lat/embed/Gdbv8BEkZhOFZ2O"
_REFERER = "https://shinden.pl/"

_ASSIGN_RESPONSE = {"url": "https://s1.flyfile.app", "token": "streamtok"}
_RAW_URL = "https://s1.flyfile.app/raw/streamtok"


def _make_session_patch(step_json):
    resp = MagicMock(status_code=200, **{"raise_for_status.return_value": None})
    resp.json.return_value = step_json

    session = MagicMock()
    session.get.return_value = resp
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.flyf.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolveFlyf:
    def test_happy_path_resolves_to_raw(self):
        with _make_session_patch(_ASSIGN_RESPONSE):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _RAW_URL
        assert stream.ext == "mp4"
        assert stream.headers.get("Referer") == _EMBED

    def test_assign_failure_raises(self):
        session = MagicMock()
        resp = MagicMock(status_code=500)
        resp.raise_for_status.side_effect = Exception("upstream error")
        session.get.return_value = resp
        session.__enter__ = lambda s: session
        session.__exit__ = MagicMock(return_value=False)

        with patch("alt_ani_cli.extract.flyf.cffi_requests.Session", return_value=session):
            with pytest.raises(Exception, match="upstream error"):
                resolve(_EMBED, _REFERER)

    def test_get_request_url_and_custom_headers(self):
        with _make_session_patch(_ASSIGN_RESPONSE) as session:
            resolve(_EMBED, _REFERER)

        session.get.assert_called_once()
        call = session.get.call_args
        assert call.args == ("https://api.flyfile.app/api/streaming/assign/Gdbv8BEkZhOFZ2O",)
        headers = call.kwargs["headers"]
        assert headers["Referer"] == _EMBED
        assert headers["X-FlyFile-View"] == "embed"
        assert headers["X-Embed-Referrer"] == _EMBED
        assert headers["X-Adblock-Detected"] == "0"

    def test_missing_url_or_token_raises_value_error(self):
        with _make_session_patch({"url": "https://s1.flyfile.app"}):
            with pytest.raises(ValueError, match="flyf"):
                resolve(_EMBED, _REFERER)

    def test_non_dict_response_raises_value_error(self):
        with _make_session_patch(["unexpected"]):
            with pytest.raises(ValueError, match="flyf"):
                resolve(_EMBED, _REFERER)

    def test_bad_embed_url_raises_before_http(self):
        with _make_session_patch(_ASSIGN_RESPONSE) as session:
            with pytest.raises(ValueError, match="flyf"):
                resolve("not-a-url", _REFERER)
        session.get.assert_not_called()
