"""Tests for global error handlers in cli.main()."""

from unittest.mock import patch

import httpx
import pytest

from alt_ani_cli.cli import main


def _make_http_error(status_code: int, url: str = "https://shinden.pl/series/1-test") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


@pytest.mark.unit
class TestHTTPStatusErrorHandler:
    def test_503_prints_status_and_exits_1(self, capsys):
        exc = _make_http_error(503)
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", "https://shinden.pl/series/1-test"]),
            patch("alt_ani_cli.cli._run_noninteractive", side_effect=exc),
            patch("alt_ani_cli.cli.shinden_http.make_client"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "503" in captured.err or "503" in captured.out

    def test_404_prints_url_and_exits_1(self, capsys):
        url = "https://shinden.pl/series/99-missing"
        exc = _make_http_error(404, url)
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", url]),
            patch("alt_ani_cli.cli._run_noninteractive", side_effect=exc),
            patch("alt_ani_cli.cli.shinden_http.make_client"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        combined = captured.err + captured.out
        assert "404" in combined
        assert "shinden.pl" in combined
