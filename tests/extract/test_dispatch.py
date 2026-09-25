"""Tests for extract/__init__.py — _normalize_url and resolve() dispatch."""

import io
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests import exceptions as cffi_exceptions
from yt_dlp import YoutubeDL
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError as YtdlpHTTPError
from yt_dlp.networking.exceptions import TransportError

from alt_ani_cli.errors import JavaScriptRequiredError, NoStreamError, UnsupportedHostError
from alt_ani_cli.extract import HOST_RULES, HostRule, _normalize_url, resolve
from alt_ani_cli.extract.common import CATEGORY_NO_STREAM_URL, CATEGORY_PARSER_DRIFT, ExtractError, Stream

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
        event, host, exc_text = on_fallback.call_args[0]
        assert event == "extractor_fallback"
        assert host == "mp4upload.com"
        assert exc_text == "ValueError: parse error"

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
        event, host, exc_text = on_fallback.call_args[0]
        assert event == "jwplayer_fallback"
        assert host == "unknownhost.tv"
        assert exc_text == "ValueError: no url"

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

    def test_viewdara_hosts_route_to_vidara_extractor(self):
        from alt_ani_cli.extract import vidara

        assert HOST_RULES["viewdara.com"] == HostRule("custom", vidara.resolve)
        assert HOST_RULES["www.viewdara.com"] == HostRule("custom", vidara.resolve)

    def test_vidara_lookalike_hosts_route_to_vidara_extractor(self):
        """vidawra/vidwara/vidvara mirrors confirmed to speak the same /api/stream protocol."""
        from alt_ani_cli.extract import vidara

        for host in ("vidawra.cc", "vidawra.co", "vidwara.site", "vidvara.biz"):
            assert HOST_RULES[host] == HostRule("custom", vidara.resolve)

    def test_morningmarkets_hosts_route_to_vidara_extractor(self):
        """Cosmetic JWPlayer skin, but the real source comes from vidara's /api/stream."""
        from alt_ani_cli.extract import vidara

        assert HOST_RULES["morningmarkets.art"] == HostRule("custom", vidara.resolve)
        assert HOST_RULES["morningmarkets.fit"] == HostRule("custom", vidara.resolve)

    def test_dood_yt_routes_to_dood_extractor(self):
        from alt_ani_cli.extract import dood

        assert HOST_RULES["dood.yt"] == HostRule("custom", dood.resolve)

    def test_lycoris_hosts_route_to_lycoris_extractor(self):
        from alt_ani_cli.extract import lycoris

        assert HOST_RULES["lycoris.cafe"] == HostRule("custom", lycoris.resolve)
        assert HOST_RULES["www.lycoris.cafe"] == HostRule("custom", lycoris.resolve)

    def test_playmate_hosts_route_to_playmate_extractor(self):
        from alt_ani_cli.extract import playmate

        assert HOST_RULES["playmate.to"] == HostRule("custom", playmate.resolve)
        assert HOST_RULES["www.playmate.to"] == HostRule("custom", playmate.resolve)

    def test_uqload_hosts_route_to_uqload_extractor(self):
        from alt_ani_cli.extract import uqload

        assert HOST_RULES["uqload.is"] == HostRule("custom", uqload.resolve)
        assert HOST_RULES["www.uqload.is"] == HostRule("custom", uqload.resolve)

    def test_flyf_hosts_route_to_flyf_extractor(self):
        from alt_ani_cli.extract import flyf

        assert HOST_RULES["flyf.lat"] == HostRule("custom", flyf.resolve)
        assert HOST_RULES["www.flyf.lat"] == HostRule("custom", flyf.resolve)

    def test_morencius_hosts_route_to_jwplayer(self):
        assert HOST_RULES["morencius.com"] == HostRule("jwplayer")
        assert HOST_RULES["www.morencius.com"] == HostRule("jwplayer")

    def test_pixeldrain_hosts_route_to_ytdlp(self):
        assert HOST_RULES["pixeldrain.com"].mode == "ytdlp"
        assert HOST_RULES["www.pixeldrain.com"].mode == "ytdlp"

    def test_mega_hosts_marked_unsupported_as_encrypted(self):
        assert HOST_RULES["mega.nz"] == HostRule("unsupported", reason="encrypted_host")
        assert HOST_RULES["www.mega.nz"] == HostRule("unsupported", reason="encrypted_host")

    def test_mega_fails_fast_without_extraction_attempts(self):
        on_fallback = MagicMock()
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve") as mock_jw,
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve") as mock_ytdlp,
        ):
            with pytest.raises(UnsupportedHostError, match="mega.nz") as exc_info:
                resolve("https://mega.nz/embed/#!abc!key", _REFERER, on_fallback=on_fallback)
        assert not isinstance(exc_info.value, JavaScriptRequiredError)
        mock_jw.assert_not_called()
        mock_ytdlp.assert_not_called()
        on_fallback.assert_not_called()

    def test_failure_messages_use_host_not_embed_url(self):
        embed_url = "https://unknownhost.tv/embed/abc"
        on_fallback = MagicMock()
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ValueError(f"no video URL in {embed_url!r}")),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception(f"Unsupported URL: {embed_url}")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve(embed_url, _REFERER, on_fallback=on_fallback)
        assert embed_url not in str(exc_info.value)
        assert "unknownhost.tv" in str(exc_info.value)
        exc_text = on_fallback.call_args[0][2]
        assert embed_url not in exc_text
        assert "unknownhost.tv" in exc_text


def _http_error(status_code: int) -> cffi_exceptions.HTTPError:
    resp = MagicMock(status_code=status_code)
    return cffi_exceptions.HTTPError(f"HTTP {status_code}", response=resp)


@pytest.mark.unit
class TestFailureDiagnosticsAttrs:
    def test_unsupported_host_tags_layer_and_category(self):
        with pytest.raises(UnsupportedHostError) as exc_info:
            resolve("https://mega.nz/embed/abc", _REFERER)
        assert exc_info.value.layer == "unsupported"
        assert exc_info.value.category == "unsupported_host"
        assert exc_info.value.http_status is None
        assert exc_info.value.used_fallback is False

    def test_custom_extractor_tagged_category_propagates_when_ytdlp_also_fails(self):
        failing_fn = MagicMock(side_effect=ExtractError("bad shape", CATEGORY_PARSER_DRIFT))
        with (
            patch.dict("alt_ani_cli.extract.HOST_RULES", {"mp4upload.com": HostRule("custom", failing_fn)}),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://mp4upload.com/embed-abc.html", _REFERER)
        assert exc_info.value.layer == "custom"
        assert exc_info.value.category == CATEGORY_PARSER_DRIFT
        assert exc_info.value.used_fallback is True
        assert exc_info.value.fallback_layer == "ytdlp"
        assert exc_info.value.fallback_category == "unknown"

    def test_jwplayer_rule_mode_tags_layer_jwplayer(self):
        with (
            patch.dict("alt_ani_cli.extract.HOST_RULES", {"streamwish.com": HostRule("jwplayer")}),
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ExtractError("no url", CATEGORY_NO_STREAM_URL)),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://streamwish.com/e/abc", _REFERER)
        assert exc_info.value.layer == "jwplayer"
        assert exc_info.value.category == CATEGORY_NO_STREAM_URL

    def test_unknown_host_both_fail_tags_layer_jwplayer_from_first_attempt(self):
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=_http_error(403)),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("Unsupported URL")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://unknownhost.tv/embed/abc", _REFERER)
        assert exc_info.value.layer == "jwplayer"
        assert exc_info.value.category == "http_error"
        assert exc_info.value.http_status == 403
        assert exc_info.value.used_fallback is True
        assert exc_info.value.fallback_layer == "ytdlp"

    def test_unsupported_host_has_no_fallback_fields(self):
        with pytest.raises(UnsupportedHostError) as exc_info:
            resolve("https://mega.nz/embed/abc", _REFERER)
        assert exc_info.value.fallback_layer is None
        assert exc_info.value.fallback_category is None
        assert exc_info.value.fallback_http_status is None

    def test_timeout_classified_as_timeout_category(self):
        with (
            patch.dict(
                "alt_ani_cli.extract.HOST_RULES",
                {"mp4upload.com": HostRule("custom", MagicMock(side_effect=cffi_exceptions.ConnectTimeout("timed out")))},
            ),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://mp4upload.com/embed-abc.html", _REFERER)
        assert exc_info.value.category == "timeout"

    def test_dns_failure_classified_as_network_error_category(self):
        with (
            patch.dict(
                "alt_ani_cli.extract.HOST_RULES",
                {
                    "mp4upload.com": HostRule(
                        "custom", MagicMock(side_effect=cffi_exceptions.DNSError("could not resolve host"))
                    )
                },
            ),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://mp4upload.com/embed-abc.html", _REFERER)
        assert exc_info.value.category == "network_error"

    def test_json_decode_failure_classified_as_parser_drift(self):
        json_exc = cffi_exceptions.JSONDecodeError("Expecting value", "<html>...</html>", 0)
        with (
            patch.dict(
                "alt_ani_cli.extract.HOST_RULES", {"mp4upload.com": HostRule("custom", MagicMock(side_effect=json_exc))}
            ),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://mp4upload.com/embed-abc.html", _REFERER)
        assert exc_info.value.category == "parser_drift"

    def test_unclassifiable_exception_falls_back_to_unknown(self):
        with (
            patch.dict(
                "alt_ani_cli.extract.HOST_RULES",
                {"mp4upload.com": HostRule("custom", MagicMock(side_effect=RuntimeError("boom")))},
            ),
            patch("alt_ani_cli.extract.ytdlp_resolver.resolve", side_effect=Exception("ytdlp fail")),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://mp4upload.com/embed-abc.html", _REFERER)
        assert exc_info.value.category == "unknown"


def _ytdlp_transport_raises(exc: Exception):
    """Fails yt-dlp at its transport layer so its real extractor and exception wrapping still run."""

    def _urlopen(self, req, *args, **kwargs):
        raise exc

    return patch.object(YoutubeDL, "urlopen", _urlopen)


def _ytdlp_http_error(status: int) -> YtdlpHTTPError:
    return YtdlpHTTPError(Response(io.BytesIO(b""), "https://example.invalid/", {}, status=status))


@pytest.mark.unit
class TestRealYtdlpFailureDiagnostics:
    def test_ytdlp_only_route_keeps_http_status_through_real_wrapping(self):
        with _ytdlp_transport_raises(_ytdlp_http_error(403)):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://www.cda.pl/video/abc123", _REFERER)
        assert exc_info.value.layer == "ytdlp"
        assert exc_info.value.category == "http_error"
        assert exc_info.value.http_status == 403
        assert exc_info.value.used_fallback is False
        assert exc_info.value.fallback_layer is None

    def test_ytdlp_only_route_timeout_is_not_flattened(self):
        with _ytdlp_transport_raises(TransportError(cause=TimeoutError("timed out"))):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://www.cda.pl/video/abc123", _REFERER)
        assert exc_info.value.category == "timeout"

    def test_unknown_host_keeps_primary_and_fallback_causes_separately(self):
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ExtractError("no url", CATEGORY_PARSER_DRIFT)),
            _ytdlp_transport_raises(TransportError(cause=ConnectionResetError("reset"))),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://unknownhost.tv/embed/abc", _REFERER)
        err = exc_info.value
        assert (err.layer, err.category, err.http_status) == ("jwplayer", CATEGORY_PARSER_DRIFT, None)
        assert (err.fallback_layer, err.fallback_category, err.fallback_http_status) == ("ytdlp", "network_error", None)
        assert err.used_fallback is True

    def test_unknown_host_fallback_http_status_is_kept(self):
        with (
            patch("alt_ani_cli.extract.jwplayer.resolve", side_effect=ExtractError("no url", CATEGORY_PARSER_DRIFT)),
            _ytdlp_transport_raises(_ytdlp_http_error(503)),
        ):
            with pytest.raises(NoStreamError) as exc_info:
                resolve("https://unknownhost.tv/embed/abc", _REFERER)
        assert exc_info.value.category == CATEGORY_PARSER_DRIFT
        assert exc_info.value.fallback_category == "http_error"
        assert exc_info.value.fallback_http_status == 503
