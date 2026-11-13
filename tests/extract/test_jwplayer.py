"""Tests for Dean Edwards p,a,c,k,e,d packer decoder in jwplayer extractor."""

import pytest

from alt_ani_core.extract.jwplayer import _best_hls_url, _decode_base_n, _normalize_stream_url, resolve, unpack_packer
from alt_ani_core.models import StreamProtocol
from alt_ani_core.transport.api import EXTRACTOR_PROFILE

_SIMPLE_PACKED = (
    "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
    "('0([{3:\"4\"}]);',10,5,'sources|x|y|file|https://cdn.example.com/v.m3u8|'.split('|')))"
    "</script>"
)


@pytest.mark.unit
class TestUnpackPacker:
    def test_finds_sources_url(self, fake_session):
        assert "https://cdn.example.com/v.m3u8" in unpack_packer(_SIMPLE_PACKED)

    def test_noop_when_no_packer(self, fake_session):
        html = "<script>var x = 1;</script>"
        assert unpack_packer(html) == html

    def test_appends_decoded_without_losing_original(self, fake_session):
        result = unpack_packer(_SIMPLE_PACKED)
        assert "<script>" in result
        assert "https://cdn.example.com/v.m3u8" in result

    def test_handles_escaped_quotes_in_packed(self, fake_session):
        packed = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
            r"('0(\"1\");',10,2,'sources|https://cdn.example.com/video.m3u8'.split('|')))"
            "</script>"
        )
        assert "https://cdn.example.com/video.m3u8" in unpack_packer(packed)

    def test_empty_key_keeps_word(self, fake_session):
        # Empty key at position 1 → the word "1" stays as "1"
        packed = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}('0 1;',10,2,'sources|'.split('|')))</script>"
        )
        result = unpack_packer(packed)
        assert "sources" in result
        assert " 1;" in result or "1;" in result

    def test_decodes_packer_call_without_trailing_split(self, fake_session):
        # Some hosts (morencius.com) call eval(...)('packed',base,count,'keys') without the
        # usual trailing .split('|') — the keys string is still pipe-delimited either way.
        packed = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
            "('0([{3:\"4\"}]);',10,5,'sources|x|y|file|https://cdn.example.com/no-split.m3u8|'))"
            "</script>"
        )
        assert "https://cdn.example.com/no-split.m3u8" in unpack_packer(packed)

    def test_base_62_packer_decodes_uppercase_digit_index(self, fake_session):
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
    def test_base_36_and_below_delegates_to_builtin_int(self, fake_session):
        assert _decode_base_n("z", 36) == int("z", 36)
        assert _decode_base_n("10", 10) == 10

    def test_base_62_digit_values(self, fake_session):
        assert _decode_base_n("a", 62) == 10
        assert _decode_base_n("A", 62) == 36
        assert _decode_base_n("Z", 62) == 61
        assert _decode_base_n("10", 62) == 62

    def test_invalid_digit_raises_value_error(self, fake_session):
        with pytest.raises(ValueError, match="invalid packer digit"):
            _decode_base_n("!", 62)


@pytest.mark.unit
class TestBestHlsUrl:
    def test_picks_hls4_when_multiple_present(self, fake_session):
        html = '"hls2":"https://x/low.m3u8","hls4":"https://x/best.m3u8","hls3":"https://x/mid.m3u8"'
        assert _best_hls_url(html) == "https://x/best.m3u8"

    def test_falls_back_to_bare_hls_key(self, fake_session):
        html = '"hls":"https://x/only.m3u8"'
        assert _best_hls_url(html) == "https://x/only.m3u8"

    def test_none_when_no_hls_key(self, fake_session):
        assert _best_hls_url("no hls here") is None


@pytest.mark.unit
class TestNormalizeStreamUrl:
    def test_unescapes_json_escaped_slashes(self, fake_session):
        url = _normalize_stream_url(r"https:\/\/cdn.example\/foo.m3u8", "https://morencius.com/embed/abc")
        assert url == "https://cdn.example/foo.m3u8"

    def test_resolves_relative_path_against_embed_url(self, fake_session):
        url = _normalize_stream_url(r"\/foo\/master.m3u8", "https://morencius.com/embed/abc")
        assert url == "https://morencius.com/foo/master.m3u8"

    def test_absolute_url_without_escaping_is_unchanged(self, fake_session):
        url = _normalize_stream_url("https://cdn.example/foo.m3u8", "https://morencius.com/embed/abc")
        assert url == "https://cdn.example/foo.m3u8"


_EMBED = "https://morencius.com/embed/uwpbf2bnjip9"
_REFERER = "https://shinden.pl/"

# Morencius-shaped packed payload: hls2/hls3/hls4 keys with JSON-escaped slashes and a
# host-relative hls4 path — regression fixture for the escaping/urljoin fix. Crucially,
# this omits the trailing .split('|') that the generic packer call normally has: the
# morencius.com variant relies on the eval'd function to split the keys string itself,
# so the outer packer regex must not require .split('|') to be present.
_MORENCIUS_PACKED_KEYS = [
    *(f"k{i}" for i in range(36)),
    "hls2",
    "https:\\/\\/cdn.example\\/low.m3u8",
    "hls3",
    "https:\\/\\/cdn.example\\/mid.m3u8",
    "hls4",
    "\\/hls\\/best\\/master.m3u8",
]
_MORENCIUS_PACKED_BODY = (
    "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};return p}"
    '(\'"A":"B","C":"D","E":"F"\',62,'
    f"{len(_MORENCIUS_PACKED_KEYS)},'{'|'.join(_MORENCIUS_PACKED_KEYS)}'))"
    "</script>"
)


@pytest.mark.unit
class TestResolve:
    def test_morencius_shaped_packed_hls_is_unescaped_and_joined(self, fake_session):
        session = fake_session(get=[_MORENCIUS_PACKED_BODY])
        stream = resolve(_EMBED, _REFERER, session.transport)
        # hls4 wins (highest tier), escaped slashes are unescaped, and the host-relative
        # path is resolved against the embed URL's origin.
        assert stream.default.url == "https://morencius.com/hls/best/master.m3u8"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_opens_one_extractor_session(self, fake_session):
        session = fake_session(get=[_MORENCIUS_PACKED_BODY])
        resolve(_EMBED, _REFERER, session.transport)
        session.transport.open.assert_called_once_with(EXTRACTOR_PROFILE)

    def test_sources_mp4_is_progressive(self, fake_session):
        session = fake_session(get=['sources: [{file:"https://cdn.example/v/video.mp4?t=abc"}]'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.example/v/video.mp4?t=abc"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"
        assert stream.default.headers["Referer"] == _EMBED

    def test_sources_m3u8_is_hls(self, fake_session):
        session = fake_session(get=['sources: [{file:"https://cdn.example/v/master.m3u8"}]'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_sources_extensionless_path_ignores_the_query(self, fake_session):
        session = fake_session(get=['sources: [{file:"https://cdn.example/dl/abc?next=list.m3u8"}]'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.example/dl/abc?next=list.m3u8"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_sources_hls_directory_without_m3u8_suffix_is_hls(self, fake_session):
        session = fake_session(get=['sources: [{file:"https://cdn.example/hls/abc/master.txt?name=video.mp4"}]'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_standalone_file_key_protocol_follows_the_path(self, fake_session):
        session = fake_session(get=['var cfg = {file: "https://cdn.example/media/episode-01.mp4"};'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.example/media/episode-01.mp4"
        assert stream.default.protocol is StreamProtocol.PROGRESSIVE
        assert stream.default.container == "mp4"

    def test_hls_key_is_hls_even_when_the_path_does_not_say_so(self, fake_session):
        session = fake_session(get=['"hls2":"https://cdn.example/play/abc?file=index.m3u8"'])
        stream = resolve(_EMBED, _REFERER, session.transport)
        assert stream.default.url == "https://cdn.example/play/abc?file=index.m3u8"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None

    def test_no_video_url_raises_value_error(self, fake_session):
        session = fake_session(get=["<html><body>nothing here</body></html>"])
        with pytest.raises(ValueError, match="jwplayer") as exc_info:
            resolve(_EMBED, _REFERER, session.transport)
        assert exc_info.value.category == "parser_drift"


@pytest.mark.unit
class TestResolveJwplayerFixture:
    def test_morencius_packed_page_resolves_hls4_relative_path(self, fake_session, load_fixture):
        html = load_fixture("jwplayer_morencius_packed.html")
        # Words live only in the packer key table, so the stream URL can only come out of the real unpacker.
        assert '"hls4"' not in html and "master.m3u8" not in html
        session = fake_session(get=[html])

        stream = resolve(_EMBED, _REFERER, session.transport)

        assert stream.default.url == "https://morencius.com/stream/testfilecode/testtoken/1700000000/master.m3u8"
        assert stream.default.protocol is StreamProtocol.HLS
        assert stream.default.container is None
        assert stream.default.headers["Referer"] == _EMBED
        assert "User-Agent" in stream.default.headers

        call = session.get.call_args
        assert call.args == (_EMBED,)
        assert call.kwargs["headers"]["Referer"] == _REFERER
