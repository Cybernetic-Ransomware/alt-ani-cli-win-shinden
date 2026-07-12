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

    def test_custom_extractor_failure_reports_fallback_and_falls_back_to_ytdlp(self):
        failing_fn = MagicMock(side_effect=ValueError("parse error"))
        on_fallback = MagicMock()
        with (
            patch.dict("alt_ani_cli.extract._CUSTOM", {"mp4upload.com": failing_fn}),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", return_value=_STREAM),
        ):
            result = resolve("https://mp4upload.com/embed-abc.html", _REFERER, on_fallback=on_fallback)
        assert result is _STREAM
        on_fallback.assert_called_once()
        event, host, exc = on_fallback.call_args[0]
        assert event == "extractor_fallback"
        assert host == "mp4upload.com"
        assert isinstance(exc, ValueError)

    def test_custom_extractor_failure_without_callback_still_falls_back(self):
        failing_fn = MagicMock(side_effect=ValueError("parse error"))
        with (
            patch.dict("alt_ani_cli.extract._CUSTOM", {"mp4upload.com": failing_fn}),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", return_value=_STREAM),
        ):
            assert resolve("https://mp4upload.com/embed-abc.html", _REFERER) is _STREAM

    def test_custom_and_ytdlp_both_fail_raises_no_stream_error(self):
        failing_fn = MagicMock(side_effect=ValueError("parse error"))
        with (
            patch.dict("alt_ani_cli.extract._CUSTOM", {"mp4upload.com": failing_fn}),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError):
                resolve("https://mp4upload.com/embed-abc.html", _REFERER)

    def test_unknown_host_jwplayer_fails_reports_fallback_then_ytdlp_succeeds(self):
        on_fallback = MagicMock()
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ValueError("no url")),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", return_value=_STREAM),
        ):
            result = resolve("https://unknownhost.tv/embed/abc", _REFERER, on_fallback=on_fallback)
        assert result is _STREAM
        on_fallback.assert_called_once()
        event, host, exc = on_fallback.call_args[0]
        assert event == "jwplayer_fallback"
        assert host == "unknownhost.tv"
        assert isinstance(exc, ValueError)

    def test_vidara_hosts_route_to_vidara_extractor(self):
        from alt_ani_cli.extract import _CUSTOM, vidara

        assert _CUSTOM["vidara.to"] is vidara.resolve
        assert _CUSTOM["www.vidara.to"] is vidara.resolve

    def test_lycoris_hosts_route_to_lycoris_extractor(self):
        from alt_ani_cli.extract import _CUSTOM, lycoris

        assert _CUSTOM["lycoris.cafe"] is lycoris.resolve
        assert _CUSTOM["www.lycoris.cafe"] is lycoris.resolve

    def test_unknown_host_both_fail_raises_no_stream_error(self):
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ValueError("no url")),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError):
                resolve("https://unknownhost.tv/embed/abc", _REFERER)
