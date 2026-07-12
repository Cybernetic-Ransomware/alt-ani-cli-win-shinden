"""Tests for global error handlers in cli.main()."""

from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError

from alt_ani_cli.cli import main
from alt_ani_cli.errors import FilterMismatchError


def _make_http_error(status_code: int, url: str = "https://shinden.pl/series/1-test") -> CurlHTTPError:
    resp = MagicMock()
    resp.status_code = status_code
    resp.url = url
    return CurlHTTPError(f"HTTP {status_code}", response=resp)


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

    def test_403_shinden_no_flaresolverr_shows_cloudflare_hint(self, capsys):
        exc = _make_http_error(403)
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", "https://shinden.pl/series/1-test"]),
            patch("alt_ani_cli.cli._run_noninteractive", side_effect=exc),
            patch("alt_ani_cli.cli.shinden_http.make_client"),
            patch("alt_ani_cli.cli.FLARESOLVERR_URL", ""),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Cloudflare" in captured.err + captured.out

    def test_403_shinden_with_flaresolverr_shows_unreachable_hint(self, capsys):
        exc = _make_http_error(403)
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", "https://shinden.pl/series/1-test"]),
            patch("alt_ani_cli.cli._run_noninteractive", side_effect=exc),
            patch("alt_ani_cli.cli.shinden_http.make_client"),
            patch("alt_ani_cli.cli.FLARESOLVERR_URL", "http://localhost:8191"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "localhost:8191" in captured.err + captured.out


@pytest.mark.unit
class TestFilterMismatchErrorHandler:
    def test_filter_mismatch_exits_1_with_message(self, capsys):
        exc = FilterMismatchError("No players match the requested filters (--lang=xx).")
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", "https://shinden.pl/series/1-test", "-S", "1", "-e", "1"]),
            patch("alt_ani_cli.cli._run_noninteractive", side_effect=exc),
            patch("alt_ani_cli.cli.shinden_http.make_client"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--lang=xx" in captured.err + captured.out
