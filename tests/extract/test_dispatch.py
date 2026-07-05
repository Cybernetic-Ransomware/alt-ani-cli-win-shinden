"""Tests for extract/__init__.py — _normalize_url and resolve() dispatch."""

from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.errors import NoStreamError
from alt_ani_cli.extract import _normalize_url, resolve
from alt_ani_cli.extract.common import Stream

_REFERER = "https://shinden.pl/"
_STREAM = Stream(url="https://example.com/video.mp4")


@pytest.mark.unit
class TestNormalizeUrl:
    def test_ebd_cda_rewritten_to_www_cda(self):
        result = _normalize_url("https://ebd.cda.pl/800x450/abc12")
        assert result == "https://www.cda.pl/video/abc12"

    def test_ebd_cda_non_matching_path_unchanged(self):
        url = "https://ebd.cda.pl/watch/abc"
        assert _normalize_url(url) == url

    def test_non_cda_host_passthrough(self):
        url = "https://mp4upload.com/embed-abc.html"
        assert _normalize_url(url) == url

    def test_host_case_insensitive(self):
        result = _normalize_url("https://EBD.cda.pl/620x395/xyz9")
        assert result == "https://www.cda.pl/video/xyz9"


@pytest.mark.unit
class TestResolveDispatch:
    def test_js_only_host_raises_no_stream_error(self):
        with pytest.raises(NoStreamError, match="voe.sx"):
            resolve("https://voe.sx/embed/abc", _REFERER)

    def test_custom_extractor_failure_warns_and_falls_back_to_ytdlp(self):
        failing_fn = MagicMock(side_effect=ValueError("parse error"))
        with (
            patch.dict("alt_ani_cli.extract._CUSTOM", {"mp4upload.com": failing_fn}),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", return_value=_STREAM),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = resolve("https://mp4upload.com/embed-abc.html", _REFERER)
        assert result is _STREAM
        mock_warn.assert_called_once()
        assert "mp4upload.com" in mock_warn.call_args[0][0]

    def test_custom_and_ytdlp_both_fail_raises_no_stream_error(self):
        failing_fn = MagicMock(side_effect=ValueError("parse error"))
        with (
            patch.dict("alt_ani_cli.extract._CUSTOM", {"mp4upload.com": failing_fn}),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            with pytest.raises(NoStreamError):
                resolve("https://mp4upload.com/embed-abc.html", _REFERER)

    def test_unknown_host_jwplayer_fails_warns_then_ytdlp_succeeds(self):
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ValueError("no url")),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", return_value=_STREAM),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = resolve("https://unknownhost.tv/embed/abc", _REFERER)
        assert result is _STREAM
        mock_warn.assert_called_once()
        assert "unknownhost.tv" in mock_warn.call_args[0][0]

    def test_unknown_host_both_fail_raises_no_stream_error(self):
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ValueError("no url")),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            with pytest.raises(NoStreamError):
                resolve("https://unknownhost.tv/embed/abc", _REFERER)
