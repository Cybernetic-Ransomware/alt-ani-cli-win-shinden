"""Tests for shinden/http.py — cf_clearance cache and FlareSolverr integration."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.shinden.http import (
    _fetch_via_flaresolverr,
    _get_clearance,
    _load_cached,
    _save_cached,
)


def _urlopen_mock(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _flaresolverr_ok(cf_value: str = "clearance123", ua: str = "TestUA", expires: float | None = None) -> dict:
    return {
        "status": "ok",
        "solution": {
            "userAgent": ua,
            "cookies": [
                {
                    "name": "cf_clearance",
                    "value": cf_value,
                    "expires": expires if expires is not None else time.time() + 3600,
                }
            ],
        },
    }


@pytest.mark.unit
class TestLoadCached:
    def test_returns_none_when_file_missing(self, tmp_path):
        with patch("alt_ani_cli.shinden.http._CF_CACHE", tmp_path / "cf.json"):
            assert _load_cached() is None

    def test_returns_values_when_valid(self, tmp_path):
        cache = tmp_path / "cf.json"
        cache.write_text(json.dumps({"cf_clearance": "abc", "user_agent": "UA", "expires": time.time() + 3600}))
        with patch("alt_ani_cli.shinden.http._CF_CACHE", cache):
            assert _load_cached() == ("abc", "UA")

    def test_returns_none_when_expired(self, tmp_path):
        cache = tmp_path / "cf.json"
        cache.write_text(json.dumps({"cf_clearance": "abc", "user_agent": "UA", "expires": time.time() - 1}))
        with patch("alt_ani_cli.shinden.http._CF_CACHE", cache):
            assert _load_cached() is None

    def test_returns_none_on_invalid_json(self, tmp_path):
        cache = tmp_path / "cf.json"
        cache.write_text("not json")
        with patch("alt_ani_cli.shinden.http._CF_CACHE", cache):
            assert _load_cached() is None


@pytest.mark.unit
class TestSaveCached:
    def test_writes_correct_json(self, tmp_path):
        cache = tmp_path / "cf.json"
        with (
            patch("alt_ani_cli.shinden.http._CF_CACHE", cache),
            patch("alt_ani_cli.shinden.http.CACHE_DIR", tmp_path),
        ):
            _save_cached("val", "UA", 9999.0)
        assert json.loads(cache.read_text()) == {"cf_clearance": "val", "user_agent": "UA", "expires": 9999.0}

    def test_creates_missing_cache_dir(self, tmp_path):
        subdir = tmp_path / "sub"
        cache = subdir / "cf.json"
        with (
            patch("alt_ani_cli.shinden.http._CF_CACHE", cache),
            patch("alt_ani_cli.shinden.http.CACHE_DIR", subdir),
        ):
            _save_cached("v", "u", 1.0)
        assert cache.exists()

    def test_silently_ignores_write_error(self, tmp_path):
        bad_path = tmp_path / "cf.json"
        bad_path.mkdir()  # directory where a file is expected — write_text will fail
        with (
            patch("alt_ani_cli.shinden.http._CF_CACHE", bad_path),
            patch("alt_ani_cli.shinden.http.CACHE_DIR", tmp_path),
        ):
            _save_cached("v", "u", 1.0)  # must not raise


@pytest.mark.unit
class TestFetchViaFlaresolverr:
    def test_returns_clearance_and_ua_on_success(self):
        mock_resp = _urlopen_mock(_flaresolverr_ok())
        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
            patch("alt_ani_cli.shinden.http._save_cached"),
        ):
            result = _fetch_via_flaresolverr()
        assert result == ("clearance123", "TestUA")

    def test_returns_none_on_connection_error(self):
        with (
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
        ):
            assert _fetch_via_flaresolverr() is None

    def test_returns_none_when_status_not_ok(self):
        mock_resp = _urlopen_mock({"status": "error", "message": "something went wrong"})
        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
        ):
            assert _fetch_via_flaresolverr() is None

    def test_returns_none_when_no_cf_clearance_cookie(self):
        data = {
            "status": "ok",
            "solution": {
                "userAgent": "UA",
                "cookies": [{"name": "other_cookie", "value": "x", "expires": 9999.0}],
            },
        }
        mock_resp = _urlopen_mock(data)
        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
        ):
            assert _fetch_via_flaresolverr() is None

    def test_session_cookie_defaults_to_one_hour(self):
        mock_resp = _urlopen_mock(_flaresolverr_ok(expires=-1))
        saved_expires: list[float] = []

        def capture_save(val, ua, expires):
            saved_expires.append(expires)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
            patch("alt_ani_cli.shinden.http._save_cached", side_effect=capture_save),
        ):
            _fetch_via_flaresolverr()

        assert saved_expires[0] == pytest.approx(time.time() + 3600, abs=5)

    def test_saves_clearance_to_cache_on_success(self, tmp_path):
        mock_resp = _urlopen_mock(_flaresolverr_ok())
        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
            patch("alt_ani_cli.shinden.http._save_cached") as mock_save,
        ):
            _fetch_via_flaresolverr()
        mock_save.assert_called_once()
        args = mock_save.call_args[0]
        assert args[0] == "clearance123"
        assert args[1] == "TestUA"


@pytest.mark.unit
class TestGetClearance:
    def test_returns_cache_without_calling_flaresolverr(self):
        with (
            patch("alt_ani_cli.shinden.http._load_cached", return_value=("val", "UA")),
            patch("alt_ani_cli.shinden.http._fetch_via_flaresolverr") as mock_fetch,
        ):
            result = _get_clearance()
        assert result == ("val", "UA")
        mock_fetch.assert_not_called()

    def test_returns_none_when_no_cache_and_no_url(self):
        with (
            patch("alt_ani_cli.shinden.http._load_cached", return_value=None),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", ""),
        ):
            assert _get_clearance() is None

    def test_calls_flaresolverr_on_cache_miss(self):
        with (
            patch("alt_ani_cli.shinden.http._load_cached", return_value=None),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
            patch("alt_ani_cli.shinden.http._fetch_via_flaresolverr", return_value=("v", "UA")) as mock_fetch,
            patch("builtins.print"),
        ):
            result = _get_clearance()
        assert result == ("v", "UA")
        mock_fetch.assert_called_once()

    def test_prints_message_when_calling_flaresolverr(self):
        with (
            patch("alt_ani_cli.shinden.http._load_cached", return_value=None),
            patch("alt_ani_cli.shinden.http.FLARESOLVERR_URL", "http://localhost:8191"),
            patch("alt_ani_cli.shinden.http._fetch_via_flaresolverr", return_value=None),
            patch("builtins.print") as mock_print,
        ):
            _get_clearance()
        mock_print.assert_called_once()
        assert "FlareSolverr" in mock_print.call_args[0][0]
