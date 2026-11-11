"""Quality selection on Stream.select: the chosen variant carries its own URL, headers, protocol and container."""

import pytest
from alt_ani_core.models import Stream, StreamProtocol, StreamVariant
from alt_ani_core.testing import make_stream

_P = StreamProtocol.PROGRESSIVE


def _variant(label: str, url: str | None = None, **kwargs) -> StreamVariant:
    kwargs.setdefault("protocol", _P)
    return StreamVariant(url=url or f"https://cdn.example.com/video-{label or 'default'}.mp4", label=label, **kwargs)


_V360 = _variant("360p")
_V720 = _variant("720p")
_V1080 = _variant("1080p")
_LADDER = Stream(variants=(_V720, _V1080, _V360))


@pytest.mark.unit
class TestStreamSelect:
    def test_best_picks_greatest_height(self):
        assert _LADDER.select("best") is _V1080

    def test_worst_picks_smallest_height(self):
        assert _LADDER.select("worst") is _V360

    def test_height_is_compared_numerically_not_as_text(self):
        stream = Stream(variants=(_variant("720p"), _variant("1080p"), _variant("2160p")))
        assert stream.select("best").label == "2160p"
        assert stream.select("worst").label == "720p"

    def test_exact_label(self):
        assert _LADDER.select("720p") is _V720

    def test_unknown_label_returns_default(self):
        stream = Stream(variants=(_V720, _V1080, _V360), default_index=2)
        assert stream.select("480p") is _V360

    def test_stream_without_labelled_variants_returns_default(self):
        stream = Stream.single("https://cdn.example.com/video.mp4", _P, container="mp4")
        for quality in ("best", "worst", "source-mkv"):
            assert stream.select(quality) is stream.default

    def test_unlabelled_default_is_not_a_best_or_worst_candidate(self):
        unlabelled = _variant("", url="https://cdn.example.com/video.mp4")
        stream = Stream(variants=(unlabelled, _V720, _V360))
        assert stream.select("best") is _V720
        assert stream.select("worst") is _V360
        assert stream.select("480p") is unlabelled

    def test_non_height_label_ranks_below_any_height(self):
        source = _variant("source-mkv", url="https://cdn.example.com/video-source.mkv", container="mkv")
        stream = Stream(variants=(source, _V720))
        assert stream.select("best") is _V720
        assert stream.select("worst") is source


@pytest.mark.unit
class TestSelectedVariantProperties:
    def test_source_mkv_variant_has_mkv_container(self):
        stream = Stream(
            variants=(
                _variant("1080p", url="https://cdn.example.com/video-1080p.mp4", container="mp4"),
                _variant("source-mkv", url="https://cdn.example.com/video-source.mkv", container="mkv"),
            )
        )

        result = stream.select("source-mkv")

        assert result.url == "https://cdn.example.com/video-source.mkv"
        assert result.container == "mkv"

    def test_mp4_variant_keeps_mp4_when_default_is_mkv(self):
        stream = Stream(
            variants=(
                _variant("", url="https://cdn.example.com/video-source.mkv", container="mkv"),
                _variant("1080p", url="https://cdn.example.com/video-1080p.mp4", container="mp4"),
            )
        )

        result = stream.select("1080p")

        assert result.url == "https://cdn.example.com/video-1080p.mp4"
        assert result.container == "mp4"

    def test_hls_variant_keeps_its_protocol_and_query_string(self):
        hls = _variant("hls", url="https://cdn.example.com/master.m3u8?token=deadbeef", protocol=StreamProtocol.HLS)
        stream = Stream(variants=(_variant("", url="https://cdn.example.com/video.mp4", container="mp4"), hls))

        result = stream.select("hls")

        assert result.url == "https://cdn.example.com/master.m3u8?token=deadbeef"
        assert result.protocol is StreamProtocol.HLS
        assert result.container is None

    def test_headers_are_those_of_the_selected_variant(self):
        low = _variant("360p", headers={"Referer": "https://a.example/"})
        high = _variant("1080p", headers={"Referer": "https://b.example/", "Cookie": "k=v"})
        stream = Stream(variants=(low, high))

        assert stream.select("best").headers == {"Referer": "https://b.example/", "Cookie": "k=v"}
        assert stream.select("worst").headers == {"Referer": "https://a.example/"}

    def test_flat_shaped_stream_keeps_extension_for_url_without_one(self):
        stream = make_stream(
            "https://cdn.example.com/video.mp4", qualities={"raw": "https://cdn.example.com/download?id=123"}, ext="mp4"
        )

        result = stream.select("raw")

        assert result.url == "https://cdn.example.com/download?id=123"
        assert result.container == "mp4"


@pytest.mark.unit
class TestStreamInvariants:
    def test_labels_lists_only_labelled_variants_in_order(self):
        stream = Stream(variants=(_variant(""), _V720, _V360))
        assert stream.labels == ("720p", "360p")

    def test_empty_stream_is_rejected(self):
        with pytest.raises(ValueError, match="at least one variant"):
            Stream(variants=())

    def test_default_outside_variants_is_rejected(self):
        with pytest.raises(ValueError, match="default"):
            Stream(variants=(_V720,), default_index=1)
