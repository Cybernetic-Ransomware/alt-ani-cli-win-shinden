import base64
import re

import httpx

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.extract.common import Stream

_FILE_RE = re.compile(
    r'["\']?(?:file|src)["\']?\s*:\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']'
)
_PLAYER_SRC_RE = re.compile(r'player\.src\s*\(\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']')


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

    html = resp.text

    m = _PLAYER_SRC_RE.search(html) or _FILE_RE.search(html)
    if m:
        return Stream(
            url=m.group(1),
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext=_ext(m.group(1)),
        )

    # Some mp4upload variants base64-encode the URL
    b64_match = re.search(r'"file"\s*:\s*"([A-Za-z0-9+/=]{30,})"', html)
    if b64_match:
        try:
            decoded = base64.b64decode(b64_match.group(1)).decode("utf-8")
            if decoded.startswith("http"):
                return Stream(
                    url=decoded,
                    headers={"Referer": embed_url, "User-Agent": USER_AGENT},
                    ext=_ext(decoded),
                )
        except Exception:
            pass

    raise ValueError(f"mp4upload: cannot find video URL in {embed_url!r}")


def _ext(url: str) -> str:
    return "m3u8" if "m3u8" in url else "mp4"
