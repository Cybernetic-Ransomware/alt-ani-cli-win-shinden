"""Tests for extract/ytdlp_resolver.py."""

from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.ytdlp_resolver import resolve

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

    def test_extract_info_exception_is_wrapped_without_category(self):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.__exit__.return_value = False
        ydl.extract_info.side_effect = RuntimeError("Unsupported URL")
        with patch("yt_dlp.YoutubeDL", return_value=ydl):
            with pytest.raises(ValueError, match="Unsupported URL") as exc_info:
                resolve(_EMBED, _REFERER)
        assert getattr(exc_info.value, "category", None) is None
