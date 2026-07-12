"""Tests for extract/streamtape.py — stream URL resolver."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.streamtape import resolve

_EMBED = "https://streamtape.com/e/abc123"
_REFERER = "https://shinden.pl/"


def _make_session_patch(html: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    resp.raise_for_status.return_value = None

    session = MagicMock()
    session.get.return_value = resp
    session.__enter__ = lambda s: session
    session.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _ctx():
        with patch("alt_ani_cli.extract.streamtape.cffi_requests.Session", return_value=session):
            yield session

    return _ctx()


_ROBOTLINK_PROTO_REL = """
<script>
var robotlink = document.getElementById('robotlink');
robotlink.innerHTML = '//streamtape.com/get_video?id=abc' + '&expires=1&ip=x&token=tok';
</script>
"""

_ROBOTLINK_RELATIVE = """
<script>
var robotlink = document.getElementById('robotlink');
robotlink.innerHTML = '/get_video?id=a' + '&t=b';
</script>
"""

_DIRECT_URL = """
<script>
var url = "https://streamtape.com/get_video?id=zz&token=tt";
</script>
"""

_NO_MATCH = "<html><body>no video here</body></html>"


@pytest.mark.unit
class TestResolveStreamtape:
    def test_robotlink_protocol_relative_prefixed_with_https(self):
        with _make_session_patch(_ROBOTLINK_PROTO_REL):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == "https://streamtape.com/get_video?id=abc&expires=1&ip=x&token=tok"
        assert stream.ext == "mp4"
        assert stream.headers.get("Referer") == _EMBED

    def test_robotlink_relative_path_prefixed_with_host(self):
        with _make_session_patch(_ROBOTLINK_RELATIVE):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == "https://streamtape.com/get_video?id=a&t=b"

    def test_direct_url_fallback(self):
        with _make_session_patch(_DIRECT_URL):
            stream = resolve(_EMBED, _REFERER)
        assert stream.url == "https://streamtape.com/get_video?id=zz&token=tt"
        assert stream.headers.get("Referer") == _EMBED

    def test_no_match_raises_value_error(self):
        with _make_session_patch(_NO_MATCH):
            with pytest.raises(ValueError, match="streamtape"):
                resolve(_EMBED, _REFERER)
