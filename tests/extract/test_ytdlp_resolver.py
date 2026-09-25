"""Tests for extract/ytdlp_resolver.py."""

import io
from unittest.mock import MagicMock, patch

import pytest
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError, RegexNotFoundError, UnsupportedError

from alt_ani_cli.extract.ytdlp_resolver import _classify_ytdlp_error, resolve

_EMBED = "https://example.com/embed/abc"
_REFERER = "https://shinden.pl/"


def _patch_ydl(info):
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.__exit__.return_value = False
    ydl.extract_info.return_value = info
    return patch("yt_dlp.YoutubeDL", return_value=ydl)


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
        assert stream.url == "https://cdn.example.com/1080p.mp4"
        assert stream.qualities == {"480p": "https://cdn.example.com/480p.mp4", "1080p": "https://cdn.example.com/1080p.mp4"}

    def test_no_info_raises_no_stream_url_category(self):
        with _patch_ydl(None):
            with pytest.raises(ValueError) as exc_info:
                resolve(_EMBED, _REFERER)
        assert exc_info.value.category == "no_stream_url"

    def test_no_usable_url_raises_no_stream_url_category(self):
        with _patch_ydl({"formats": []}):
            with pytest.raises(ValueError) as exc_info:
                resolve(_EMBED, _REFERER)
        assert exc_info.value.category == "no_stream_url"

    def test_unrecognised_extract_info_exception_is_wrapped_as_unknown(self):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.__exit__.return_value = False
        ydl.extract_info.side_effect = RuntimeError("boom")
        with patch("yt_dlp.YoutubeDL", return_value=ydl):
            with pytest.raises(ValueError, match="boom") as exc_info:
                resolve(_EMBED, _REFERER)
        assert exc_info.value.category == "unknown"
        assert exc_info.value.http_status is None


def _download_error(inner: BaseException) -> DownloadError:
    return DownloadError("wrapped", exc_info=(type(inner), inner, None))


@pytest.mark.unit
class TestClassifyYtdlpError:
    @pytest.mark.parametrize(
        ("inner", "expected"),
        [
            (ExtractorError("x", cause=HTTPError(Response(io.BytesIO(b""), "https://e.invalid/", {}, status=404))), ("http_error", 404)),
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
