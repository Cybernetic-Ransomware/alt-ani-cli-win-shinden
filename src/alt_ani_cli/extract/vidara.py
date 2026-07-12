import re

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream

_EMBED_RE = re.compile(r"(https?://[^/]+)/(?:e|d|f|v)/([A-Za-z0-9]+)")


def resolve(embed_url: str, referer: str) -> Stream:
    m = _EMBED_RE.match(embed_url)
    if not m:
        raise ValueError(EXCEPTIONS["vidara"]["bad_embed_url"].format(embed_url=repr(embed_url)))
    base, filecode = m.group(1), m.group(2)

    with cffi_requests.Session(impersonate="chrome", timeout=30.0, allow_redirects=True) as client:
        resp = client.post(
            f"{base}/api/stream",
            json={"filecode": filecode, "device": "web"},
            headers={
                "Origin": base,
                "Referer": embed_url,
                "User-Agent": USER_AGENT,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    url = data.get("streaming_url") if isinstance(data, dict) else None
    if not url:
        raise ValueError(EXCEPTIONS["vidara"]["no_stream_url"].format(embed_url=repr(embed_url)))

    return Stream(
        url=url,
        headers={"Referer": base + "/", "Origin": base, "User-Agent": USER_AGENT},
        ext="m3u8" if "m3u8" in url else "mp4",
    )
