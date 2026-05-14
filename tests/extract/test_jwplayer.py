"""Tests for Dean Edwards p,a,c,k,e,d packer decoder in jwplayer extractor."""

import pytest

from alt_ani_cli.extract.jwplayer import _unpack_packer

_SIMPLE_PACKED = (
    "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
    "('0([{3:\"4\"}]);',10,5,'sources|x|y|file|https://cdn.example.com/v.m3u8|'.split('|')))"
    "</script>"
)


@pytest.mark.unit
class TestUnpackPacker:
    def test_finds_sources_url(self):
        assert "https://cdn.example.com/v.m3u8" in _unpack_packer(_SIMPLE_PACKED)

    def test_noop_when_no_packer(self):
        html = "<script>var x = 1;</script>"
        assert _unpack_packer(html) == html

    def test_appends_decoded_without_losing_original(self):
        result = _unpack_packer(_SIMPLE_PACKED)
        assert "<script>" in result
        assert "https://cdn.example.com/v.m3u8" in result

    def test_handles_escaped_quotes_in_packed(self):
        packed = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
            r"('0(\"1\");',10,2,'sources|https://cdn.example.com/video.m3u8'.split('|')))"
            "</script>"
        )
        assert "https://cdn.example.com/video.m3u8" in _unpack_packer(packed)

    def test_empty_key_keeps_word(self):
        # Empty key at position 1 → the word "1" stays as "1"
        packed = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
            "('0 1;',10,2,'sources|'.split('|')))"
            "</script>"
        )
        result = _unpack_packer(packed)
        assert "sources" in result
        assert " 1;" in result or "1;" in result
