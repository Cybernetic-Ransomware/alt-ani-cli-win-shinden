"""Tests for extract/dood.py — stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

from alt_ani_cli.extract.dood import resolve

_EMBED = "https://dood.la/e/xyz"
_REFERER = "https://shinden.pl/"

_PAGE_WITH_TOKEN = """
<script>
$.get('/pass_md5/12/abc-DEF_9', function(data) { makePlay(data); });
makePlay('?token=tok123abc');
</script>
"""

_PAGE_NO_TOKEN = """
<script>
$.get('/pass_md5/12/abc-DEF_9', function(data) { makePlay(data); });
</script>
"""

_PAGE_NO_PASS_MD5 = "<html><body>no pass_md5 here</body></html>"

_PASS_MD5_RESPONSE = "https://d1.dood.video/seg"


def _make_session_patch(responses: list[str]):
    resps = [
        MagicMock(status_code=200, text=t, **{"raise_for_status.return_value": None})
        for t in responses
    ]
    session = MagicMock()
    session.get.side_effect = resps
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.dood.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


@pytest.mark.unit
class TestResolveDood:
    def test_happy_path_url_and_headers(self):
        with (
            _make_session_patch([_PAGE_WITH_TOKEN, _PASS_MD5_RESPONSE]) as session,
            patch("alt_ani_cli.extract.dood.time.time", return_value=1000.0),
        ):
            stream = resolve(_EMBED, _REFERER)

        assert stream.url == "https://d1.dood.video/seg?token=tok123abc&expiry=1000000"
        assert stream.headers.get("Referer") == "https://dood.la/"
        assert stream.ext == "mp4"

    def test_happy_path_second_request_url_and_headers(self):
        with (
            _make_session_patch([_PAGE_WITH_TOKEN, _PASS_MD5_RESPONSE]) as session,
            patch("alt_ani_cli.extract.dood.time.time", return_value=1000.0),
        ):
            resolve(_EMBED, _REFERER)

        second_call = session.get.call_args_list[1]
        assert second_call == call(
            "https://dood.la/pass_md5/12/abc-DEF_9",
            headers={
                "Referer": _EMBED,
                "User-Agent": second_call.kwargs["headers"]["User-Agent"],
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def test_no_token_in_page_url_has_no_query(self):
        with (
            _make_session_patch([_PAGE_NO_TOKEN, _PASS_MD5_RESPONSE]),
            patch("alt_ani_cli.extract.dood.time.time", return_value=1000.0),
        ):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == _PASS_MD5_RESPONSE

    def test_no_pass_md5_raises_value_error_after_one_request(self):
        with _make_session_patch([_PAGE_NO_PASS_MD5]) as session:
            with pytest.raises(ValueError, match="dood"):
                resolve(_EMBED, _REFERER)
        assert session.get.call_count == 1

    def test_bad_base_url_raises_before_http(self):
        with _make_session_patch([]) as session:
            with pytest.raises(ValueError, match="dood"):
                resolve("not-a-url", _REFERER)
        session.get.assert_not_called()
