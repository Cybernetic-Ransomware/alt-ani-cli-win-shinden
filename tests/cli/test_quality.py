"""Tests for CLI stream quality selection."""

import pytest

from alt_ani_cli.cli import _pick_quality
from alt_ani_cli.extract.common import Stream


@pytest.mark.unit
class TestPickQuality:
    def test_source_mkv_updates_ext_to_mkv(self):
        stream = Stream(
            url="https://cdn.example.com/video-1080p.mp4",
            qualities={
                "1080p": "https://cdn.example.com/video-1080p.mp4",
                "source-mkv": "https://cdn.example.com/video-source.mkv",
            },
            ext="mp4",
        )

        result = _pick_quality(stream, "source-mkv")

        assert result.url == "https://cdn.example.com/video-source.mkv"
        assert result.ext == "mkv"

    def test_mp4_quality_keeps_ext_mp4(self):
        stream = Stream(
            url="https://cdn.example.com/video-source.mkv",
            qualities={"1080p": "https://cdn.example.com/video-1080p.mp4"},
            ext="mkv",
        )

        result = _pick_quality(stream, "1080p")

        assert result.url == "https://cdn.example.com/video-1080p.mp4"
        assert result.ext == "mp4"

    def test_m3u8_quality_ignores_query_string(self):
        stream = Stream(
            url="https://cdn.example.com/video.mp4",
            qualities={"hls": "https://cdn.example.com/master.m3u8?token=deadbeef"},
            ext="mp4",
        )

        result = _pick_quality(stream, "hls")

        assert result.url == "https://cdn.example.com/master.m3u8?token=deadbeef"
        assert result.ext == "m3u8"

    def test_unknown_extension_uses_fallback_ext(self):
        stream = Stream(
            url="https://cdn.example.com/video.mp4",
            qualities={"raw": "https://cdn.example.com/download?id=123"},
            ext="mp4",
        )

        result = _pick_quality(stream, "raw")

        assert result.url == "https://cdn.example.com/download?id=123"
        assert result.ext == "mp4"

    def test_stream_without_qualities_returns_same_instance(self):
        stream = Stream(url="https://cdn.example.com/video.mp4", ext="mp4")

        result = _pick_quality(stream, "source-mkv")

        assert result is stream
