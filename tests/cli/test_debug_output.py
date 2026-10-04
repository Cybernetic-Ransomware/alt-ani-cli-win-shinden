"""--debug rendering: secrets are redacted at display time only."""

import io
from unittest.mock import patch

import pytest
from rich.console import Console

from alt_ani_cli.cli import _print_debug
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.models import EmbedURL
from alt_ani_cli.player import mpv

_URL = "https://cdn.example/x.mp4?token=SECRET"
_QUALITIES = {"1080p": "https://cdn.example/x1080.mp4?sig=ABC"}
_HEADERS = {
    "Authorization": "Bearer VERY_SECRET",
    "Cookie": "session=VERY_SECRET_TOO",
    "Referer": "https://embed.example/e/1?token=REF_SECRET",
    "User-Agent": "test-agent",
}
_EMBED = EmbedURL(url="https://embed.example/e/1?token=EMBED_SECRET", referer="https://shinden.pl/")


def _stream() -> Stream:
    return Stream(url=_URL, headers=dict(_HEADERS), qualities=dict(_QUALITIES), ext="mp4")


def _render(stream: Stream) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=300, legacy_windows=False, highlight=False)
    with patch("alt_ani_cli.ui.progress._get", return_value=console):
        _print_debug(stream, _EMBED)
    return buf.getvalue()


@pytest.mark.unit
class TestPrintDebugRedaction:
    def test_no_secret_in_output(self):
        out = _render(_stream())
        for secret in ("SECRET", "ABC", "VERY_SECRET", "VERY_SECRET_TOO", "REF_SECRET", "EMBED_SECRET", "Bearer"):
            assert secret not in out

    def test_hosts_and_paths_visible(self):
        out = _render(_stream())
        assert "cdn.example/x.mp4?token=<redacted>" in out
        assert "cdn.example/x1080.mp4?sig=<redacted>" in out
        assert "embed.example/e/1?token=<redacted>" in out
        assert "<redacted>" in out

    def test_user_agent_still_visible(self):
        assert "test-agent" in _render(_stream())


@pytest.mark.unit
class TestRedactionLeavesRuntimeDataIntact:
    def test_print_debug_does_not_mutate_stream(self):
        stream = _stream()
        _render(stream)
        assert stream.url == _URL
        assert stream.qualities == _QUALITIES
        assert stream.headers == _HEADERS
        assert _EMBED.url == "https://embed.example/e/1?token=EMBED_SECRET"

    def test_player_command_still_gets_raw_url_and_headers(self):
        stream = _stream()
        _render(stream)
        with patch("alt_ani_cli.player.mpv._find", return_value="mpv"):
            cmd = mpv.build(stream, title="t")
        assert _URL in cmd
        assert "--referrer=https://embed.example/e/1?token=REF_SECRET" in cmd
        fields = next(a for a in cmd if a.startswith("--http-header-fields="))
        assert "Authorization: Bearer VERY_SECRET" in fields
        assert "Cookie: session=VERY_SECRET_TOO" in fields
