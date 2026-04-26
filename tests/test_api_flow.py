"""Critical test: time.sleep must be called BETWEEN player_load and player_show."""
import pytest
import respx
import httpx
from unittest.mock import patch, call

from alt_ani_cli.shinden.api import resolve_embed
from alt_ani_cli.config import SHINDEN_API_BASE, GUEST_AUTH
from alt_ani_cli.errors import AntiBotError

_BASE = f"{SHINDEN_API_BASE}/xhr/42"
_AUTH = f"auth={GUEST_AUTH}"
_LOAD_URL = f"{_BASE}/player_load?{_AUTH}"
_SHOW_URL = f"{_BASE}/player_show?{_AUTH}&width=0&height=-1"
_IFRAME_HTML = '<html><body><iframe src="//video.sibnet.ru/shell.php?videoid=9999"></iframe></body></html>'


@respx.mock
def test_sleep_called_between_load_and_show():
    respx.get(_LOAD_URL).mock(return_value=httpx.Response(200, text="ok"))
    respx.get(_SHOW_URL).mock(return_value=httpx.Response(200, text=_IFRAME_HTML))

    call_order: list[str] = []

    def fake_sleep(secs):
        call_order.append("sleep")

    original_get = httpx.Client.get

    def tracking_get(self, url, **kw):
        if "player_load" in url:
            call_order.append("player_load")
        elif "player_show" in url:
            call_order.append("player_show")
        return original_get(self, url, **kw)

    with patch("alt_ani_cli.shinden.api.time.sleep", side_effect=fake_sleep), \
         patch("httpx.Client.get", autospec=True, side_effect=tracking_get):
        client = httpx.Client()
        result = resolve_embed(client, "42", sleep_seconds=0.0)

    assert call_order == ["player_load", "sleep", "player_show"], (
        f"Expected [player_load, sleep, player_show], got {call_order}"
    )
    assert result.url == "https://video.sibnet.ru/shell.php?videoid=9999"


@respx.mock
def test_sleep_called_with_configured_delay():
    respx.get(_LOAD_URL).mock(return_value=httpx.Response(200, text="ok"))
    respx.get(_SHOW_URL).mock(return_value=httpx.Response(200, text=_IFRAME_HTML))

    with patch("alt_ani_cli.shinden.api.time.sleep") as mock_sleep:
        client = httpx.Client()
        resolve_embed(client, "42", sleep_seconds=7.0)

    mock_sleep.assert_called_once_with(7.0)


@respx.mock
def test_player_load_403_raises_antibot():
    respx.get(_LOAD_URL).mock(return_value=httpx.Response(403))

    with pytest.raises(AntiBotError, match="player_load"):
        resolve_embed(httpx.Client(), "42")


@respx.mock
def test_player_show_403_raises_antibot():
    respx.get(_LOAD_URL).mock(return_value=httpx.Response(200, text="ok"))
    respx.get(_SHOW_URL).mock(return_value=httpx.Response(403))

    with patch("alt_ani_cli.shinden.api.time.sleep"):
        with pytest.raises(AntiBotError, match="player_show"):
            resolve_embed(httpx.Client(), "42")


@respx.mock
def test_missing_iframe_raises_antibot():
    respx.get(_LOAD_URL).mock(return_value=httpx.Response(200, text="ok"))
    respx.get(_SHOW_URL).mock(return_value=httpx.Response(200, text="<html><body></body></html>"))

    with patch("alt_ani_cli.shinden.api.time.sleep"):
        with pytest.raises(AntiBotError, match="no iframe"):
            resolve_embed(httpx.Client(), "42")


@respx.mock
def test_protocol_prefix_added_to_relative_src():
    html = '<iframe src="//cdn.example.com/embed/abc"></iframe>'
    respx.get(_LOAD_URL).mock(return_value=httpx.Response(200, text="ok"))
    respx.get(_SHOW_URL).mock(return_value=httpx.Response(200, text=html))

    with patch("alt_ani_cli.shinden.api.time.sleep"):
        result = resolve_embed(httpx.Client(), "42")

    assert result.url.startswith("https://")
