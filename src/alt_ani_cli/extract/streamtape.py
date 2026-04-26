import re

import httpx

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.extract.common import Stream

# Streamtape concatenates two JS strings to form the /get_video token URL.
# Pattern: robotlink.innerHTML = '/get_video?id=...' + '&expires=...&ip=...'
_ROBOT_RE = re.compile(
    r"robotlink[\"']?\)?\.innerHTML\s*=\s*[\"']([^\"']+)[\"']\s*\+\s*[\"']([^\"']+)[\"']"
)
_DIRECT_RE = re.compile(r'"(https?://streamtape\.[a-z]+/get_video[^"]+)"')


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

    m = _ROBOT_RE.search(html)
    if m:
        path = m.group(1) + m.group(2).strip()
        if path.startswith("//"):
            path = "https:" + path
        elif not path.startswith("http"):
            path = "https://streamtape.com" + path
        return Stream(
            url=path,
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext="mp4",
        )

    m2 = _DIRECT_RE.search(html)
    if m2:
        return Stream(
            url=m2.group(1),
            headers={"Referer": embed_url, "User-Agent": USER_AGENT},
            ext="mp4",
        )

    raise ValueError(f"streamtape: cannot parse token from {embed_url!r}")
