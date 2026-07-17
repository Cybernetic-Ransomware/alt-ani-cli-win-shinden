"""Tests for extract/__init__.py — _normalize_url and resolve() dispatch."""

from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.errors import JavaScriptRequiredError, NoStreamError, UnsupportedHostError
from alt_ani_cli.extract import HOST_RULES, HostRule, _normalize_url, resolve
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
class TestErrorHierarchy:
    def test_js_required_is_unsupported_host(self):
        assert issubclass(JavaScriptRequiredError, UnsupportedHostError)

    def test_unsupported_host_is_no_stream(self):
        assert issubclass(UnsupportedHostError, NoStreamError)


@pytest.mark.unit
class TestResolveDispatch:
    def test_js_only_host_raises_js_required_error(self):
        with pytest.raises(JavaScriptRequiredError, match="voe.sx"):
            resolve("https://voe.sx/embed/abc", _REFERER)

    def test_unsupported_host_without_reason_raises_unsupported_error(self):
        rule = HostRule("unsupported")
        with patch.dict("alt_ani_cli.extract.HOST_RULES", {"deadhost.tv": rule}):
            with pytest.raises(UnsupportedHostError, match="deadhost.tv"):
                resolve("https://deadhost.tv/embed/abc", _REFERER)

    def test_ytdlp_host_skips_jwplayer_and_reports_no_fallback(self):
        on_fallback = MagicMock()
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve") as mock_jw,
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", return_value=_STREAM) as mock_ytdlp,
        ):
            result = resolve("https://pixeldrain.com/u/abc123", _REFERER, on_fallback=on_fallback)
        assert result is _STREAM
        mock_jw.assert_not_called()
        mock_ytdlp.assert_called_once()
        on_fallback.assert_not_called()

    def test_ytdlp_host_failure_raises_no_stream_error(self):
        with patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")):
            with pytest.raises(NoStreamError, match="ytdlp fail"):
                resolve("https://pixeldrain.com/u/abc123", _REFERER)

    def test_jwplayer_host_dispatches_to_jwplayer_without_fallback(self):
        on_fallback = MagicMock()
        with patch("alt_ani_cli.extract.jwplayer.resolve", return_value=_STREAM) as mock_jw:
            result = resolve("https://streamwish.com/e/abc", _REFERER, on_fallback=on_fallback)
        assert result is _STREAM
        mock_jw.assert_called_once()
        on_fallback.assert_not_called()

    def test_custom_extractor_failure_reports_fallback_and_falls_back_to_ytdlp(self):
        failing_fn = MagicMock(side_effect=ValueError("parse error"))
        on_fallback = MagicMock()
        with (
            patch.dict("alt_ani_cli.extract.HOST_RULES", {"mp4upload.com": HostRule("custom", failing_fn)}),
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
            patch.dict("alt_ani_cli.extract.HOST_RULES", {"mp4upload.com": HostRule("custom", failing_fn)}),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", return_value=_STREAM),
        ):
            assert resolve("https://mp4upload.com/embed-abc.html", _REFERER) is _STREAM

    def test_custom_and_ytdlp_both_fail_raises_no_stream_error(self):
        failing_fn = MagicMock(side_effect=ValueError("parse error"))
        with (
            patch.dict("alt_ani_cli.extract.HOST_RULES", {"mp4upload.com": HostRule("custom", failing_fn)}),
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

    def test_unknown_host_both_fail_raises_no_stream_error(self):
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ValueError("no url")),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError):
                resolve("https://unknownhost.tv/embed/abc", _REFERER)

    def test_vidara_hosts_route_to_vidara_extractor(self):
        from alt_ani_cli.extract import vidara

        assert HOST_RULES["vidara.to"] == HostRule("custom", vidara.resolve)
        assert HOST_RULES["www.vidara.to"] == HostRule("custom", vidara.resolve)

    def test_lycoris_hosts_route_to_lycoris_extractor(self):
        from alt_ani_cli.extract import lycoris

        assert HOST_RULES["lycoris.cafe"] == HostRule("custom", lycoris.resolve)
        assert HOST_RULES["www.lycoris.cafe"] == HostRule("custom", lycoris.resolve)

    def test_pixeldrain_hosts_route_to_ytdlp(self):
        assert HOST_RULES["pixeldrain.com"].mode == "ytdlp"
        assert HOST_RULES["www.pixeldrain.com"].mode == "ytdlp"
