"""cf_clearance disk cache, the FlareSolverr round-trip and the Shinden session profile built from them."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from alt_ani_core.config import SHINDEN_BASE, AccessConfig
from alt_ani_core.events import ClearanceRequested, ClearanceUnavailable
from alt_ani_core.testing import RecordingSink
from alt_ani_core.transport import clearance as clearance_module
from alt_ani_core.transport.clearance import (
    Clearance,
    fetch_via_flaresolverr,
    get_clearance,
    load_cached,
    save_cached,
    shinden_profile,
)

_SOLVER = "http://localhost:8191"
_CACHE_NAME = "cf_clearance.json"


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


def _write_cache(cache_dir, expires: float) -> None:
    (cache_dir / _CACHE_NAME).write_text(json.dumps({"cf_clearance": "abc", "user_agent": "UA", "expires": expires}))


@pytest.mark.unit
class TestLoadCached:
    def test_returns_none_when_file_missing(self, tmp_path):
        assert load_cached(tmp_path) is None

    def test_returns_values_when_valid(self, tmp_path):
        _write_cache(tmp_path, time.time() + 3600)
        assert load_cached(tmp_path) == Clearance("abc", "UA")

    def test_returns_none_when_expired(self, tmp_path):
        _write_cache(tmp_path, time.time() - 1)
        assert load_cached(tmp_path) is None

    def test_returns_none_inside_the_expiry_margin(self, tmp_path):
        _write_cache(tmp_path, time.time() + clearance_module._EXPIRY_MARGIN - 10)
        assert load_cached(tmp_path) is None

    def test_returns_none_on_invalid_json(self, tmp_path):
        (tmp_path / _CACHE_NAME).write_text("not json")
        assert load_cached(tmp_path) is None


@pytest.mark.unit
class TestSaveCached:
    def test_writes_correct_json(self, tmp_path):
        save_cached(tmp_path, Clearance("val", "UA"), 9999.0)
        assert json.loads((tmp_path / _CACHE_NAME).read_text()) == {
            "cf_clearance": "val",
            "user_agent": "UA",
            "expires": 9999.0,
        }

    def test_creates_missing_cache_dir(self, tmp_path):
        subdir = tmp_path / "sub"
        save_cached(subdir, Clearance("v", "u"), 1.0)
        assert (subdir / _CACHE_NAME).exists()

    def test_silently_ignores_write_error(self, tmp_path):
        # a directory where the file is expected makes write_text fail
        (tmp_path / _CACHE_NAME).mkdir()
        save_cached(tmp_path, Clearance("v", "u"), 1.0)

    def test_saved_value_round_trips_through_load(self, tmp_path):
        save_cached(tmp_path, Clearance("val", "UA"), time.time() + 3600)
        assert load_cached(tmp_path) == Clearance("val", "UA")


@pytest.mark.unit
class TestFetchViaFlaresolverr:
    def test_returns_clearance_and_ua_on_success(self, tmp_path):
        with patch("urllib.request.urlopen", return_value=_urlopen_mock(_flaresolverr_ok())) as urlopen:
            result = fetch_via_flaresolverr(_SOLVER, tmp_path)
        assert result == Clearance("clearance123", "TestUA")
        request = urlopen.call_args.args[0]
        assert request.full_url == f"{_SOLVER}/v1"
        assert json.loads(request.data)["url"] == f"{SHINDEN_BASE}/"

    def test_returns_none_on_connection_error(self, tmp_path):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            assert fetch_via_flaresolverr(_SOLVER, tmp_path) is None

    def test_returns_none_when_status_not_ok(self, tmp_path):
        mock_resp = _urlopen_mock({"status": "error", "message": "something went wrong"})
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert fetch_via_flaresolverr(_SOLVER, tmp_path) is None

    def test_returns_none_when_no_cf_clearance_cookie(self, tmp_path):
        data = {
            "status": "ok",
            "solution": {
                "userAgent": "UA",
                "cookies": [{"name": "other_cookie", "value": "x", "expires": 9999.0}],
            },
        }
        with patch("urllib.request.urlopen", return_value=_urlopen_mock(data)):
            assert fetch_via_flaresolverr(_SOLVER, tmp_path) is None
        assert not (tmp_path / _CACHE_NAME).exists()

    def test_session_cookie_defaults_to_one_hour(self, tmp_path):
        with (
            patch("urllib.request.urlopen", return_value=_urlopen_mock(_flaresolverr_ok(expires=-1))),
            patch.object(clearance_module, "save_cached") as mock_save,
        ):
            fetch_via_flaresolverr(_SOLVER, tmp_path)
        assert mock_save.call_args.args[2] == pytest.approx(time.time() + 3600, abs=5)

    def test_saves_clearance_to_cache_on_success(self, tmp_path):
        with (
            patch("urllib.request.urlopen", return_value=_urlopen_mock(_flaresolverr_ok())),
            patch.object(clearance_module, "save_cached") as mock_save,
        ):
            fetch_via_flaresolverr(_SOLVER, tmp_path)
        mock_save.assert_called_once()
        assert mock_save.call_args.args[:2] == (tmp_path, Clearance("clearance123", "TestUA"))

    def test_fetched_clearance_is_served_from_the_cache_afterwards(self, tmp_path):
        with patch("urllib.request.urlopen", return_value=_urlopen_mock(_flaresolverr_ok())):
            fetch_via_flaresolverr(_SOLVER, tmp_path)
        assert load_cached(tmp_path) == Clearance("clearance123", "TestUA")


@pytest.mark.unit
class TestGetClearance:
    def test_returns_cache_without_calling_flaresolverr(self, tmp_path):
        sink = RecordingSink()
        with (
            patch.object(clearance_module, "load_cached", return_value=Clearance("val", "UA")),
            patch.object(clearance_module, "fetch_via_flaresolverr") as mock_fetch,
        ):
            result = get_clearance(AccessConfig(flaresolverr_url=_SOLVER), tmp_path, sink)
        assert result == Clearance("val", "UA")
        mock_fetch.assert_not_called()
        assert sink.events == []

    @pytest.mark.parametrize("url", [None, ""])
    def test_returns_none_when_no_cache_and_no_url(self, tmp_path, url):
        sink = RecordingSink()
        with patch.object(clearance_module, "fetch_via_flaresolverr") as mock_fetch:
            assert get_clearance(AccessConfig(flaresolverr_url=url), tmp_path, sink) is None
        mock_fetch.assert_not_called()
        assert sink.events == []

    def test_calls_flaresolverr_on_cache_miss(self, tmp_path):
        with patch.object(clearance_module, "fetch_via_flaresolverr", return_value=Clearance("v", "UA")) as mock_fetch:
            result = get_clearance(AccessConfig(flaresolverr_url=_SOLVER), tmp_path)
        assert result == Clearance("v", "UA")
        mock_fetch.assert_called_once_with(_SOLVER, tmp_path)

    def test_announces_the_flaresolverr_round_trip(self, tmp_path):
        sink = RecordingSink()
        with patch.object(clearance_module, "fetch_via_flaresolverr", return_value=Clearance("v", "UA")):
            get_clearance(AccessConfig(flaresolverr_url=_SOLVER), tmp_path, sink)
        assert sink.events == [ClearanceRequested()]

    def test_failed_round_trip_is_reported_as_unavailable(self, tmp_path):
        sink = RecordingSink()
        with patch.object(clearance_module, "fetch_via_flaresolverr", return_value=None):
            assert get_clearance(AccessConfig(flaresolverr_url=_SOLVER), tmp_path, sink) is None
        assert sink.events == [ClearanceRequested(), ClearanceUnavailable()]

    def test_direct_only_policy_uses_neither_cache_nor_solver(self, tmp_path):
        _write_cache(tmp_path, time.time() + 3600)
        sink = RecordingSink()
        with patch.object(clearance_module, "fetch_via_flaresolverr") as mock_fetch:
            access = AccessConfig(flaresolverr_url=_SOLVER, use_flaresolverr=False)
            assert get_clearance(access, tmp_path, sink) is None
        mock_fetch.assert_not_called()
        assert sink.events == []


@pytest.mark.unit
class TestShindenProfile:
    def test_without_clearance_has_only_the_session_seed_cookie(self):
        profile = shinden_profile(None)
        assert profile.name == "shinden"
        assert set(profile.cookies) == {"_rnd"}
        assert int(profile.cookies["_rnd"]) == pytest.approx(time.time(), abs=5)
        assert profile.headers["Referer"] == f"{SHINDEN_BASE}/"
        assert profile.headers["Accept-Language"].startswith("pl-PL")
        assert "User-Agent" not in profile.headers

    def test_clearance_adds_the_cookie_and_its_user_agent(self):
        profile = shinden_profile(Clearance("clearance123", "TestUA"))
        assert profile.cookies["cf_clearance"] == "clearance123"
        assert "_rnd" in profile.cookies
        assert profile.headers["User-Agent"] == "TestUA"

    def test_empty_user_agent_keeps_the_transport_default(self):
        profile = shinden_profile(Clearance("clearance123", ""))
        assert profile.cookies["cf_clearance"] == "clearance123"
        assert "User-Agent" not in profile.headers

    def test_clearance_cookie_never_reaches_a_repr(self):
        clearance = Clearance("clearance123", "TestUA")
        assert "clearance123" not in repr(clearance)
        assert "clearance123" not in repr(shinden_profile(clearance))
