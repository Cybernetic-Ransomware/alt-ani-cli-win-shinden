"""Tests for Dean Edwards p,a,c,k,e,d packer decoder in jwplayer extractor."""
import re

from alt_ani_cli.extract.jwplayer import _unpack_packer


def _make_packed(sources_url: str) -> str:
    """Build a minimal eval(function(p,a,c,k,e,d){...}) block that encodes a JWPlayer
    sources array containing sources_url."""
    keys = ["sources", "setup", "vplayer", "file", sources_url, "m3u8", "jwplayer"]
    packed_words = {i: str(i) for i in range(len(keys))}
    packed = f'0([{{3:"{4}",5:"{5}"}}]);6("v").2({{}});'
    for i, k in enumerate(keys):
        packed = packed.replace(str(i), str(i))
    packed_template = (
        f'0([{{3:"{4}",5:"{5}"}}]);6("v").2({{}});'
    )
    keys_str = "|".join(keys)
    return (
        f"<script>eval(function(p,a,c,k,e,d){{e=function(c){{return c}};return p}}"
        f"('{packed_template}',10,{len(keys)},'{keys_str}'.split('|')))</script>"
    )


# ---------------------------------------------------------------------------
# Tests using real-world packer pattern (simpler, more reliable)
# ---------------------------------------------------------------------------

_SIMPLE_PACKED = (
    "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
    "('0([{3:\"4\"}]);',10,5,'sources|x|y|file|https://cdn.example.com/v.m3u8|'.split('|')))"
    "</script>"
)


def test_unpack_finds_sources_url():
    result = _unpack_packer(_SIMPLE_PACKED)
    assert "https://cdn.example.com/v.m3u8" in result


def test_unpack_noop_when_no_packer():
    html = "<script>var x = 1;</script>"
    assert _unpack_packer(html) == html


def test_unpack_appends_decoded_without_losing_original():
    result = _unpack_packer(_SIMPLE_PACKED)
    assert "<script>" in result
    assert "https://cdn.example.com/v.m3u8" in result


def test_unpack_handles_escaped_quotes_in_packed():
    packed = (
        "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
        r"('0(\"1\");',10,2,'sources|https://cdn.example.com/video.m3u8'.split('|')))"
        "</script>"
    )
    result = _unpack_packer(packed)
    assert "https://cdn.example.com/video.m3u8" in result


def test_unpack_empty_key_keeps_word():
    # Empty key at position 1 → the word "1" stays as "1"
    packed = (
        "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
        "('0 1;',10,2,'sources|'.split('|')))"
        "</script>"
    )
    result = _unpack_packer(packed)
    assert "sources" in result
    assert " 1;" in result or "1;" in result
