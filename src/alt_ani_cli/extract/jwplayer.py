"""Generic extractor for JWPlayer-based embed hosts.

Covers streamwish.com / playerwish.com / filemoon.sx and similar sites
that embed a JWPlayer or flowplayer with a sources array containing the
direct video URL.  Also handles Dean Edwards p,a,c,k,e,d packed scripts.
"""

import re
from urllib.parse import urljoin

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream

# jwplayer("id").setup({...}) or jwplayer().setup({...})
_SETUP_RE = re.compile(r"jwplayer\([^)]*\)\.setup\(\s*(\{.*?\})\s*\)", re.DOTALL)

# sources:[{file:"...",...},...] — also matches with double-quotes or no quotes
_SOURCES_RE = re.compile(
    r'"?sources"?\s*:\s*\[\s*\{[^}]*"?file"?\s*:\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']',
    re.DOTALL,
)

# file:"url" standalone
_FILE_RE = re.compile(r'"?file"?\s*:\s*["\']([^"\']{20,}\.(?:mp4|m3u8)[^"\']*)["\']')

# hls:"url" / hls2:"url" / hls3:"url" / hls4:"url" — quality-tiered HLS master URLs;
# prefer the highest-numbered one when several are present.
_HLS_RE = re.compile(r'"hls([234]?)"\s*:\s*"([^"]+\.m3u8[^"]*)"')

# Dean Edwards packer: }('packed',base,count,'k1|k2|...'.split('|'))
# The trailing .split('|') is optional — some hosts (e.g. morencius.com) ship a packer
# call that omits it and rely on the eval'd function to split internally; the keys string
# itself is still pipe-delimited either way, so group(3).split("|") below covers both.
_PACKER_RE = re.compile(
    r"""}\s*\(\s*'((?:[^'\\]|\\.)*?)'\s*,\s*(\d+)\s*,\s*\d+\s*,\s*'([^']*)'\s*(?:\.split\('\|'\))?\s*\)\s*\)""",
    re.DOTALL,
)

# Dean Edwards packer digit alphabet: 0-9, then a-z (10-35), then A-Z (36-61) — real-world
# packers commonly declare base 62, which Python's builtin int(str, base) cannot decode
# (hard-capped at base 36).
_PACKER_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _decode_base_n(word: str, base: int) -> int:
    if base <= 36:
        return int(word, base)
    n = 0
    for ch in word:
        idx = _PACKER_ALPHABET.find(ch)
        if idx < 0 or idx >= base:
            raise ValueError(f"invalid packer digit {ch!r} for base {base}")
        n = n * base + idx
    return n


def unpack_packer(html: str) -> str:
    """Decode Dean Edwards packed script and append result to html; noop if absent."""
    m = _PACKER_RE.search(html)
    if not m:
        return html
    packed = m.group(1).replace("\\'", "'")
    base = int(m.group(2))
    keys = m.group(3).split("|")

    def _lookup(match: re.Match) -> str:
        word = match.group(0)
        try:
            n = _decode_base_n(word, base)
        except ValueError:
            return word
        return keys[n] if n < len(keys) and keys[n] else word

    decoded = re.sub(r"\b\w+\b", _lookup, packed)
    return html + "\n" + decoded


def _normalize_stream_url(url: str, embed_url: str) -> str:
    """Unescape JSON-escaped slashes (\\/) and resolve host-relative paths against the embed URL."""
    return urljoin(embed_url, url.replace(r"\/", "/"))


def _best_hls_url(html: str) -> str | None:
    found: dict[str, str] = {}
    for m in _HLS_RE.finditer(html):
        found[m.group(1)] = m.group(2)
    for key in ("4", "3", "2", ""):
        if key in found:
            return found[key]
    return None


def resolve(embed_url: str, referer: str) -> Stream:
    with cffi_requests.Session(impersonate="chrome", timeout=30.0, allow_redirects=True) as client:
        resp = client.get(
            embed_url,
            headers={
                "Referer": referer,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,*/*;q=0.9",
            },
        )
        resp.raise_for_status()

    html = unpack_packer(resp.text)

    # Try sources array first (most reliable)
    m = _SOURCES_RE.search(html)
    if m:
        url = _normalize_stream_url(m.group(1), embed_url)
        return Stream(
            url=url,
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext=_ext(url),
        )

    # Try hls / hls2 / hls3 / hls4 keys
    hls_url = _best_hls_url(html)
    if hls_url:
        return Stream(
            url=_normalize_stream_url(hls_url, embed_url),
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext="m3u8",
        )

    # Try generic file key
    m = _FILE_RE.search(html)
    if m:
        url = _normalize_stream_url(m.group(1), embed_url)
        return Stream(
            url=url,
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext=_ext(url),
        )

    raise ValueError(EXCEPTIONS["jwplayer"]["no_video_url"].format(embed_url=repr(embed_url)))


def _ext(url: str) -> str:
    return "m3u8" if "m3u8" in url else "mp4"
