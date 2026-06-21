"""Critical test: time.sleep must be called BETWEEN player_load and player_show."""

from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError

from alt_ani_cli.config import GUEST_AUTH, SHINDEN_API_BASE
from alt_ani_cli.errors import AntiBotError
from alt_ani_cli.shinden.api import resolve_embed

_BASE = f"{SHINDEN_API_BASE}/xhr/42"
_AUTH = f"auth={GUEST_AUTH}"
_LOAD_URL = f"{_BASE}/player_load?{_AUTH}"
_SHOW_URL = f"{_BASE}/player_show?{_AUTH}&width=0&height=-1"
_IFRAME_HTML = '<html><body><iframe src="//video.sibnet.ru/shell.php?videoid=9999"></iframe></body></html>'


def _ok(text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


def _err(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = ""
    resp.raise_for_status.side_effect = CurlHTTPError(f"HTTP {status_code}", response=resp)
    return resp


def _client_returning(*responses) -> MagicMock:
    """Return a mock Session whose .get() yields responses in order."""
    client = MagicMock()
    client.get.side_effect = list(responses)
    return client


@pytest.mark.unit
class TestResolveEmbed:
    def test_sleep_called_between_load_and_show(self):
        call_order: list[str] = []

        def fake_sleep(secs):
            call_order.append("sleep")

        def fake_get(url, **kw):
            if "player_load" in url:
                call_order.append("player_load")
                return _ok("ok")
            if "player_show" in url:
                call_order.append("player_show")
                return _ok(_IFRAME_HTML)
            raise AssertionError(f"Unexpected URL in fake_get: {url}")

        client = MagicMock()
        client.get.side_effect = fake_get

        with patch("alt_ani_cli.shinden.api.time.sleep", side_effect=fake_sleep):
            result = resolve_embed(client, "42", sleep_seconds=0.0)

        assert call_order == ["player_load", "sleep", "player_show"], (
            f"Expected [player_load, sleep, player_show], got {call_order}"
        )
        assert result.url == "https://video.sibnet.ru/shell.php?videoid=9999"

    def test_sleep_called_with_configured_delay(self):
        client = _client_returning(_ok("ok"), _ok(_IFRAME_HTML))
        with patch("alt_ani_cli.shinden.api.time.sleep") as mock_sleep:
            resolve_embed(client, "42", sleep_seconds=7.0)
        mock_sleep.assert_called_once_with(7.0)

    def test_player_load_403_raises_antibot(self):
        client = _client_returning(_err(403))
        with pytest.raises(AntiBotError, match="player_load"):
            resolve_embed(client, "42")

    def test_player_show_403_raises_antibot(self):
        client = _client_returning(_ok("ok"), _err(403))
        with patch("alt_ani_cli.shinden.api.time.sleep"), pytest.raises(AntiBotError, match="player_show"):
            resolve_embed(client, "42")

    def test_missing_iframe_raises_antibot(self):
        client = _client_returning(_ok("ok"), _ok("<html><body></body></html>"))
        with patch("alt_ani_cli.shinden.api.time.sleep"), pytest.raises(AntiBotError, match="no iframe"):
            resolve_embed(client, "42")

    def test_protocol_prefix_added_to_relative_src(self):
        html = '<iframe src="//cdn.example.com/embed/abc"></iframe>'
        client = _client_returning(_ok("ok"), _ok(html))
        with patch("alt_ani_cli.shinden.api.time.sleep"):
            result = resolve_embed(client, "42")
        assert result.url.startswith("https://")
