"""Tests for extract/common.py — protocol sniffing and container choice shared by the extractors."""

import pytest

from alt_ani_core.extract.common import CATEGORY_PARSER_DRIFT, ExtractError, container_of, single_stream, sniff_protocol
from alt_ani_core.models import StreamProtocol


@pytest.mark.unit
class TestSniffProtocol:
    @pytest.mark.parametrize(
        ("url", "protocol"),
        [
            ("https://cdn.example/v/master.m3u8", StreamProtocol.HLS),
            ("https://cdn.example/v/MASTER.M3U8?token=abc", StreamProtocol.HLS),
            ("https://cdn.example/hls/abc/master.txt?t=1", StreamProtocol.HLS),
            ("https://cdn.example/v/m3u8/abc", StreamProtocol.HLS),
            ("https://cdn.example/v/manifest.mpd?t=1", StreamProtocol.DASH),
            ("https://cdn.example/v/video.mp4", StreamProtocol.PROGRESSIVE),
            ("https://cdn.example/get_video?id=abc", StreamProtocol.PROGRESSIVE),
            ("https://cdn.example/raw/token", StreamProtocol.PROGRESSIVE),
        ],
    )
    def test_decided_by_the_path(self, url, protocol):
        assert sniff_protocol(url) is protocol

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example/dl/abc?next=list.m3u8",
            "https://cdn.example/dl/abc?manifest=x.mpd",
            "https://cdn.example/dl/abc?dir=/hls/",
            "https://cdn.example/dl/abc#master.m3u8",
        ],
    )
    def test_query_and_fragment_are_never_consulted(self, url):
        assert sniff_protocol(url) is StreamProtocol.PROGRESSIVE


@pytest.mark.unit
class TestContainerOf:
    @pytest.mark.parametrize("protocol", [StreamProtocol.HLS, StreamProtocol.DASH])
    def test_segmented_streams_have_no_container(self, protocol):
        assert container_of("https://cdn.example/v/video.mp4", protocol) is None

    @pytest.mark.parametrize(
        ("url", "container"),
        [
            ("https://cdn.example/v/video.mp4?t=1", "mp4"),
            ("https://cdn.example/v/video.MKV", "mkv"),
            ("https://cdn.example/v/video.webm", "webm"),
            ("https://cdn.example/get_video?name=video.mkv", "mp4"),
            ("https://cdn.example/v/video.avi", "mp4"),
        ],
    )
    def test_progressive_container_follows_the_path_suffix(self, url, container):
        assert container_of(url, StreamProtocol.PROGRESSIVE) == container

    def test_fallback_is_configurable(self):
        assert container_of("https://cdn.example/raw/token", StreamProtocol.PROGRESSIVE, fallback="mkv") == "mkv"


@pytest.mark.unit
class TestSingleStream:
    def test_progressive_stream_carries_headers_and_container(self):
        stream = single_stream(
            "https://cdn.example/v/video.mkv", {"Referer": "https://host.example/"}, StreamProtocol.PROGRESSIVE
        )
        assert len(stream.variants) == 1
        assert stream.default.url == "https://cdn.example/v/video.mkv"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mkv"
        assert stream.default.headers == {"Referer": "https://host.example/"}
        assert stream.default.label == ""

    def test_explicit_protocol_is_kept_even_when_the_url_disagrees(self):
        stream = single_stream("https://cdn.example/v/video.mp4", {}, StreamProtocol.HLS)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None


@pytest.mark.unit
class TestExtractError:
    def test_is_a_value_error_with_category_and_status(self):
        err = ExtractError("bad shape", CATEGORY_PARSER_DRIFT, 404)
        assert isinstance(err, ValueError)
        assert (str(err), err.category, err.http_status) == ("bad shape", "parser_drift", 404)
