import re

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.extract.jwplayer import unpack_packer

_EMBED_RE = re.compile(r"^(https?://[^/]+)/e/([A-Za-z0-9_-]+)")
_FILE_RE = re.compile(r'file\s*:\s*"([^"]+)"')


def resolve(embed_url: str, referer: str) -> Stream:
    m = _EMBED_RE.match(embed_url)
    if not m:
        raise ValueError(EXCEPTIONS["uqload"]["bad_embed_url"].format(embed_url=repr(embed_url)))
    base, file_code = m.group(1), m.group(2)

    with cffi_requests.Session(impersonate="chrome", timeout=30.0, allow_redirects=True) as client:
        resp = client.post(
            f"{base}/dl",
            data={"op": "embed", "file_code": file_code, "auto": "0", "referer": embed_url},
            headers={
                "Referer": embed_url,
                "User-Agent": USER_AGENT,
            },
        )
        resp.raise_for_status()

    html = unpack_packer(resp.text)
    m2 = _FILE_RE.search(html)
    if not m2:
        raise ValueError(EXCEPTIONS["uqload"]["no_video_url"].format(embed_url=repr(embed_url)))

    url = m2.group(1)
    return Stream(
        url=url,
        headers={"Referer": embed_url, "User-Agent": USER_AGENT},
        ext="m3u8" if "m3u8" in url else "mp4",
    )
