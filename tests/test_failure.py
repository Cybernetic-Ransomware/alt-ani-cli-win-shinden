"""failure.py — the one classifier shared by extract/ and shinden/api.py."""

import pytest

from alt_ani_core.errors import JavaScriptRequiredError, ShindenXhrError, UnsupportedHostError
from alt_ani_core.extract import common as extract_common
from alt_ani_core.extract.common import ExtractError
from alt_ani_core.rules import failure
from alt_ani_core.transport.api import DecodeError, HttpStatusError, NetworkError, TransportError, TransportTimeout


@pytest.mark.unit
class TestCategoryConstants:
    def test_extract_common_reexports_the_shared_values(self):
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
            (DecodeError(), "parser_drift"),
            (TransportTimeout(), "timeout"),
            (HttpStatusError(404), "http_error"),
            (NetworkError(), "network_error"),
            (TransportError(), "network_error"),
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

    def test_status_from_http_status_error(self):
        assert failure.http_status_of(HttpStatusError(502)) == 502

    def test_non_int_status_is_none(self):
        exc = ValueError("x")
        exc.http_status = "503"
        assert failure.http_status_of(exc) is None

    def test_no_status_is_none(self):
        assert failure.http_status_of(ValueError("x")) is None
