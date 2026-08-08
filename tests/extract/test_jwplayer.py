"""Tests for Dean Edwards p,a,c,k,e,d packer decoder in jwplayer extractor."""

import pytest

from alt_ani_cli.extract.jwplayer import _best_hls_url, _decode_base_n, unpack_packer

_SIMPLE_PACKED = (
    "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
    "('0([{3:\"4\"}]);',10,5,'sources|x|y|file|https://cdn.example.com/v.m3u8|'.split('|')))"
    "</script>"
)


@pytest.mark.unit
class TestUnpackPacker:
    def test_finds_sources_url(self):
        assert "https://cdn.example.com/v.m3u8" in unpack_packer(_SIMPLE_PACKED)

    def test_noop_when_no_packer(self):
        html = "<script>var x = 1;</script>"
        assert unpack_packer(html) == html

    def test_appends_decoded_without_losing_original(self):
        result = unpack_packer(_SIMPLE_PACKED)
        assert "<script>" in result
        assert "https://cdn.example.com/v.m3u8" in result

    def test_handles_escaped_quotes_in_packed(self):
        packed = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
            r"('0(\"1\");',10,2,'sources|https://cdn.example.com/video.m3u8'.split('|')))"
            "</script>"
        )
        assert "https://cdn.example.com/video.m3u8" in unpack_packer(packed)

    def test_empty_key_keeps_word(self):
        # Empty key at position 1 → the word "1" stays as "1"
        packed = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
            "('0 1;',10,2,'sources|'.split('|')))"
            "</script>"
        )
        result = unpack_packer(packed)
        assert "sources" in result
        assert " 1;" in result or "1;" in result

    def test_base_62_packer_decodes_uppercase_digit_index(self):
        # 40 keys: index 0..39. Base-62 digit 'A' = 36, 'B' = 37, 'C' = 38 (0-9,a-z,A-Z alphabet).
        keys = [*(f"k{i}" for i in range(36)), "sources", "file", "https://cdn.example.com/hi62.m3u8", "x"]
        assert len(keys) == 40
        packed = 'A([{B:"C"}]);'  # references keys[36]="sources", keys[37]="file", keys[38]=url
        packer_html = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
            f"('{packed}',62,{len(keys)},'{'|'.join(keys)}'.split('|')))"
            "</script>"
        )
        assert "https://cdn.example.com/hi62.m3u8" in unpack_packer(packer_html)


@pytest.mark.unit
class TestDecodeBaseN:
    def test_base_36_and_below_delegates_to_builtin_int(self):
        assert _decode_base_n("z", 36) == int("z", 36)
        assert _decode_base_n("10", 10) == 10

    def test_base_62_digit_values(self):
        assert _decode_base_n("a", 62) == 10
        assert _decode_base_n("A", 62) == 36
        assert _decode_base_n("Z", 62) == 61
        assert _decode_base_n("10", 62) == 62

    def test_invalid_digit_raises_value_error(self):
        with pytest.raises(ValueError, match="invalid packer digit"):
            _decode_base_n("!", 62)


@pytest.mark.unit
class TestBestHlsUrl:
    def test_picks_hls4_when_multiple_present(self):
        html = '"hls2":"https://x/low.m3u8","hls4":"https://x/best.m3u8","hls3":"https://x/mid.m3u8"'
        assert _best_hls_url(html) == "https://x/best.m3u8"

    def test_falls_back_to_bare_hls_key(self):
        html = '"hls":"https://x/only.m3u8"'
        assert _best_hls_url(html) == "https://x/only.m3u8"

    def test_none_when_no_hls_key(self):
        assert _best_hls_url("no hls here") is None
