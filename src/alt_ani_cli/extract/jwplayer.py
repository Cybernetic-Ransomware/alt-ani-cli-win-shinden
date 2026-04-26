"""Generic extractor for JWPlayer-based embed hosts.

Covers streamwish.com / playerwish.com / filemoon.sx and similar sites
that embed a JWPlayer or flowplayer with a sources array containing the
direct video URL.  Also handles Dean Edwards p,a,c,k,e,d packed scripts.
"""

import re

import httpx

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.extract.common import Stream

# jwplayer("id").setup({...}) or jwplayer().setup({...})
_SETUP_RE = re.compile(r"jwplayer\([^)]*\)\.setup\(\s*(\{.*?\})\s*\)", re.DOTALL)

# sources:[{file:"...",...},...] — also matches with double-quotes or no quotes
_SOURCES_RE = re.compile(
    r'"?sources"?\s*:\s*\[\s*\{[^}]*"?file"?\s*:\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']',
    re.DOTALL,
)

# file:"url" standalone
_FILE_RE = re.compile(
    r'"?file"?\s*:\s*["\']([^"\']{20,}\.(?:mp4|m3u8)[^"\']*)["\']'
)

# hls:"url"
_HLS_RE = re.compile(r'"hls"\s*:\s*"([^"]+\.m3u8[^"]*)"')

# Dean Edwards packer: }('packed',base,count,'k1|k2|...'.split('|'))
_PACKER_RE = re.compile(
    r"""}\s*\(\s*'((?:[^'\\]|\\.)*?)'\s*,\s*(\d+)\s*,\s*\d+\s*,\s*'([^']*)'\s*\.split\('\|'\)\s*\)\s*\)""",
    re.DOTALL,
)


def _unpack_packer(html: str) -> str:
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
            n = int(word, base)
        except ValueError:
            return word
        return keys[n] if n < len(keys) and keys[n] else word

    decoded = re.sub(r"\b\w+\b", _lookup, packed)
    return html + "\n" + decoded


def resolve(embed_url: str, referer: str) -> Stream:
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        resp = client.get(
            embed_url,
            headers={
                "Referer": referer,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,*/*;q=0.9",
            },
        )
        resp.raise_for_status()

    html = _unpack_packer(resp.text)

    # Try sources array first (most reliable)
    m = _SOURCES_RE.search(html)
    if m:
        url = m.group(1)
        return Stream(
            url=url,
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext=_ext(url),
        )

    # Try hls key
    m = _HLS_RE.search(html)
    if m:
        return Stream(
            url=m.group(1),
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext="m3u8",
        )

    # Try generic file key
    m = _FILE_RE.search(html)
    if m:
        url = m.group(1)
        return Stream(
            url=url,
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext=_ext(url),
        )

    raise ValueError(f"jwplayer extractor: cannot find video URL in {embed_url!r}")


def _ext(url: str) -> str:
    return "m3u8" if "m3u8" in url else "mp4"
