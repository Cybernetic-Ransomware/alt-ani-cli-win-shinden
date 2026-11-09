"""failure.py — the one classifier shared by extract/ and shinden/api.py; old import paths must keep working."""

from unittest.mock import MagicMock

import pytest
from curl_cffi.requests import exceptions as cffi_exceptions

from alt_ani_cli import extract, failure
from alt_ani_cli.errors import JavaScriptRequiredError, ShindenXhrError, UnsupportedHostError
from alt_ani_cli.extract import common as extract_common
from alt_ani_cli.extract.common import ExtractError


def _http_error(status: int) -> cffi_exceptions.HTTPError:
    resp = MagicMock()
    resp.status_code = status
    return cffi_exceptions.HTTPError(f"HTTP {status}", response=resp)


@pytest.mark.unit
class TestBackwardCompatibleImports:
    def test_extract_private_names_are_the_shared_functions(self):
        assert extract._classify is failure.classify_exception
        assert extract._http_status is failure.http_status_of

    def test_category_constants_keep_their_values_and_paths(self):
        assert extract_common.CATEGORY_PARSER_DRIFT == failure.CATEGORY_PARSER_DRIFT == "parser_drift"
        assert extract_common.CATEGORY_NO_STREAM_URL == failure.CATEGORY_NO_STREAM_URL == "no_stream_url"


@pytest.mark.unit
class TestClassifyException:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ExtractError("x", "no_stream_url"), "no_stream_url"),
            (ExtractError("x", "parser_drift", 503), "parser_drift"),
            (UnsupportedHostError("x"), "unsupported_host"),
            (JavaScriptRequiredError("x"), "unsupported_host"),
            (cffi_exceptions.JSONDecodeError("Expecting value", "<html>", 0), "parser_drift"),
            (cffi_exceptions.Timeout("t"), "timeout"),
            (cffi_exceptions.ConnectTimeout("t"), "timeout"),
            (_http_error(404), "http_error"),
            (cffi_exceptions.DNSError("d"), "network_error"),
            (cffi_exceptions.ConnectionError("c"), "network_error"),
            (cffi_exceptions.RequestException("r"), "network_error"),
            (ValueError("v"), "unknown"),
        ],
    )
    def test_categories_unchanged(self, exc, expected):
        assert failure.classify_exception(exc) == expected

    def test_xhr_error_category_is_read_from_its_tag(self):
        err = ShindenXhrError("x", stage="player_load", category="parser_drift", reason="wait_missing", http_status=200)
        assert failure.classify_exception(err) == "parser_drift"
        assert failure.http_status_of(err) == 200


@pytest.mark.unit
class TestHttpStatusOf:
    def test_tagged_status_wins(self):
        assert failure.http_status_of(ExtractError("x", "http_error", 403)) == 403

    def test_status_from_response(self):
        assert failure.http_status_of(_http_error(502)) == 502

    def test_non_int_status_is_none(self):
        exc = ValueError("x")
        exc.http_status = "503"
        assert failure.http_status_of(exc) is None

    def test_no_status_is_none(self):
        assert failure.http_status_of(ValueError("x")) is None
