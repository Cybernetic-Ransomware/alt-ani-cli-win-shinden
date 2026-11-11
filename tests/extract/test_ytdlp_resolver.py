"""Tests for extract/ytdlp_resolver.py."""

import io
from unittest.mock import MagicMock, patch

import pytest
from alt_ani_core.config import USER_AGENT, ExtractOptions
from alt_ani_core.errors import OperationCancelled, StaleSessionError
from alt_ani_core.extract.ytdlp_resolver import _classify_ytdlp_error, resolve
from alt_ani_core.models import StreamProtocol
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError, RegexNotFoundError, UnsupportedError

_EMBED = "https://example.com/embed/abc"
_REFERER = "https://shinden.pl/"


def _patch_ydl(info):
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.__exit__.return_value = False
    ydl.extract_info.return_value = info
    return patch("yt_dlp.YoutubeDL", return_value=ydl)


def _labelled(stream):
    return {v.label: v.url for v in stream.variants}


@pytest.mark.unit
class TestResolveYtdlp:
    def test_happy_path_picks_highest_quality_format(self):
        info = {
            "ext": "mp4",
            "formats": [
                {"url": "https://cdn.example.com/480p.mp4", "height": 480},
                {"url": "https://cdn.example.com/1080p.mp4", "height": 1080},
            ],
        }
        with _patch_ydl(info):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.url == "https://cdn.example.com/1080p.mp4"
        assert stream.default.label == "1080p"
        assert _labelled(stream) == {"480p": "https://cdn.example.com/480p.mp4", "1080p": "https://cdn.example.com/1080p.mp4"}
        assert stream.labels == ("480p", "1080p")
        assert all(v.protocol is StreamProtocol.PROGRESSIVE and v.container == "mp4" for v in stream.variants)

    def test_headers_default_to_referer_and_user_agent(self):
        info = {"formats": [{"url": "https://cdn.example.com/720p.mp4", "height": 720}]}
        with _patch_ydl(info):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.headers == {"Referer": _REFERER, "User-Agent": USER_AGENT}

    def test_headers_reported_by_ytdlp_win(self):
        info = {"url": "https://cdn.example.com/v.mp4", "http_headers": {"Referer": "https://host.example/", "X-Key": "1"}}
        with _patch_ydl(info):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.headers == {"Referer": "https://host.example/", "X-Key": "1", "User-Agent": USER_AGENT}

    @pytest.mark.parametrize(
        ("fmt", "protocol", "container"),
        [
            ({"protocol": "m3u8_native", "ext": "mp4"}, StreamProtocol.HLS, None),
            ({"protocol": "m3u8", "ext": "mp4"}, StreamProtocol.HLS, None),
            ({"protocol": "http_dash_segments", "ext": "mp4"}, StreamProtocol.DASH, None),
            ({"protocol": "https", "ext": "mp4"}, StreamProtocol.PROGRESSIVE, "mp4"),
            ({"protocol": "http", "ext": "webm"}, StreamProtocol.PROGRESSIVE, "webm"),
            ({"protocol": "https"}, StreamProtocol.PROGRESSIVE, "mp4"),
        ],
    )
    def test_format_protocol_comes_from_ytdlp_not_the_url(self, fmt, protocol, container):
        info = {"formats": [{"url": "https://cdn.example.com/play/abc?sig=1", "height": 720, **fmt}]}
        with _patch_ydl(info):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.protocol is protocol
        assert stream.default.container == container

    def test_progressive_protocol_wins_over_an_m3u8_looking_url(self):
        info = {"formats": [{"url": "https://cdn.example.com/hls/abc.m3u8", "height": 720, "protocol": "https", "ext": "mp4"}]}
        with _patch_ydl(info):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    @pytest.mark.parametrize(
        ("url", "protocol", "container"),
        [
            ("https://cdn.example.com/v/master.m3u8?sig=1", StreamProtocol.HLS, None),
            ("https://cdn.example.com/v/manifest.mpd", StreamProtocol.DASH, None),
            ("https://cdn.example.com/v/video.mkv", StreamProtocol.PROGRESSIVE, "mkv"),
            ("https://cdn.example.com/play/abc?file=x.m3u8", StreamProtocol.PROGRESSIVE, "mp4"),
        ],
    )
    def test_url_is_sniffed_only_when_ytdlp_names_no_protocol(self, url, protocol, container):
        with _patch_ydl({"formats": [{"url": url, "height": 720}]}):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.protocol is protocol
        assert stream.default.container == container

    def test_formats_may_mix_protocols(self):
        info = {
            "formats": [
                {"url": "https://cdn.example.com/a/480", "height": 480, "protocol": "https", "ext": "mp4"},
                {"url": "https://cdn.example.com/a/1080", "height": 1080, "protocol": "m3u8_native", "ext": "mp4"},
            ]
        }
        with _patch_ydl(info):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.label == "1080p"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.select("480p").protocol is StreamProtocol.PROGRESSIVE
        assert stream.select("480p").container == "mp4"

    def test_format_without_url_uses_its_manifest_url(self):
        info = {"formats": [{"manifest_url": "https://cdn.example.com/a/manifest", "height": 720, "protocol": "m3u8_native"}]}
        with _patch_ydl(info):
            stream = resolve(_EMBED, _REFERER)
        assert stream.default.url == "https://cdn.example.com/a/manifest"
        assert stream.default.protocol is StreamProtocol.HLS

    @pytest.mark.parametrize(
        ("info", "protocol", "container"),
        [
            ({"url": "https://cdn.example.com/play/abc", "protocol": "m3u8_native", "ext": "mp4"}, StreamProtocol.HLS, None),
            ({"url": "https://cdn.example.com/play/abc", "protocol": "http_dash_segments"}, StreamProtocol.DASH, None),
            ({"url": "https://cdn.example.com/play/abc", "protocol": "https", "ext": "mp4"}, StreamProtocol.PROGRESSIVE, "mp4"),
            ({"manifest_url": "https://cdn.example.com/play/abc.m3u8"}, StreamProtocol.HLS, None),
        ],
    )
    def test_info_without_sized_formats_yields_one_unlabelled_variant(self, info, protocol, container):
        with _patch_ydl({**info, "formats": [{"url": "https://cdn.example.com/audio", "height": None}]}):
            stream = resolve(_EMBED, _REFERER)
        assert len(stream.variants) == 1
        assert stream.labels == ()
        assert stream.default.url == (info.get("url") or info["manifest_url"])
        assert stream.default.protocol is protocol
        assert stream.default.container == container

    def test_cookie_options_reach_ytdlp(self):
        options = ExtractOptions(cookies_file="/tmp/cookies.txt", cookies_browser="firefox")
        with _patch_ydl({"url": "https://cdn.example.com/v.mp4"}) as ydl_cls:
            resolve(_EMBED, _REFERER, options)
        opts = ydl_cls.call_args.args[0]
        assert opts["cookiefile"] == "/tmp/cookies.txt"
        assert opts["cookiesfrombrowser"] == ("firefox",)
        assert opts["http_headers"]["Referer"] == _REFERER

    def test_no_cookie_options_by_default(self):
        with _patch_ydl({"url": "https://cdn.example.com/v.mp4"}) as ydl_cls:
            resolve(_EMBED, _REFERER)
        opts = ydl_cls.call_args.args[0]
        assert "cookiefile" not in opts and "cookiesfrombrowser" not in opts

    def test_no_info_raises_no_stream_url_category(self):
        with _patch_ydl(None), pytest.raises(ValueError) as exc_info:
            resolve(_EMBED, _REFERER)
        assert exc_info.value.category == "no_stream_url"

    def test_no_usable_url_raises_no_stream_url_category(self):
        with _patch_ydl({"formats": []}), pytest.raises(ValueError) as exc_info:
            resolve(_EMBED, _REFERER)
        assert exc_info.value.category == "no_stream_url"

    def test_unrecognised_extract_info_exception_is_wrapped_as_unknown(self):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.__exit__.return_value = False
        ydl.extract_info.side_effect = RuntimeError("boom")
        with patch("yt_dlp.YoutubeDL", return_value=ydl), pytest.raises(ValueError, match="boom") as exc_info:
            resolve(_EMBED, _REFERER)
        assert exc_info.value.category == "unknown"
        assert exc_info.value.http_status is None

    @pytest.mark.parametrize("interrupt", [OperationCancelled, StaleSessionError])
    def test_engine_interrupt_is_not_wrapped(self, interrupt):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.__exit__.return_value = False
        ydl.extract_info.side_effect = interrupt()
        with patch("yt_dlp.YoutubeDL", return_value=ydl), pytest.raises(interrupt):
            resolve(_EMBED, _REFERER)


def _download_error(inner: BaseException) -> DownloadError:
    return DownloadError("wrapped", exc_info=(type(inner), inner, None))


@pytest.mark.unit
class TestClassifyYtdlpError:
    @pytest.mark.parametrize(
        ("inner", "expected"),
        [
            (
                ExtractorError("x", cause=HTTPError(Response(io.BytesIO(b""), "https://e.invalid/", {}, status=404))),
                ("http_error", 404),
            ),
            (ExtractorError("x", cause=TransportError(cause=TimeoutError("t"))), ("timeout", None)),
            (ExtractorError("x", cause=TransportError(cause=ConnectionResetError("r"))), ("network_error", None)),
            (UnsupportedError("https://e.invalid/"), ("unsupported_host", None)),
            (RegexNotFoundError("Unable to extract title"), ("parser_drift", None)),
            (ExtractorError("Video unavailable", expected=True), ("unknown", None)),
        ],
    )
    def test_category_from_real_ytdlp_exception_chain(self, inner, expected):
        assert _classify_ytdlp_error(_download_error(inner)) == expected

    def test_cyclic_cause_chain_terminates(self):
        a = ExtractorError("a")
        b = ExtractorError("b", cause=a)
        a.cause = b
        assert _classify_ytdlp_error(a) == ("unknown", None)
