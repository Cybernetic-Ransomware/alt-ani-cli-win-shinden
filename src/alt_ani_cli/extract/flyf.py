import re

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream

# api.flyfile.app is a fixed API domain, distinct from the flyf.lat embed host.
_API_BASE = "https://api.flyfile.app"
_EMBED_RE = re.compile(r"^(https?://[^/]+)/embed/([A-Za-z0-9_-]+)")


def resolve(embed_url: str, referer: str) -> Stream:
    m = _EMBED_RE.match(embed_url)
    if not m:
        raise ValueError(EXCEPTIONS["flyf"]["bad_embed_url"].format(embed_url=repr(embed_url)))
    token = m.group(2)

    headers = {
        "Referer": embed_url,
        "User-Agent": USER_AGENT,
        "X-FlyFile-View": "1",
        "X-Embed-Referrer": referer,
        "X-Adblock-Detected": "false",
    }

    with cffi_requests.Session(impersonate="chrome", timeout=30.0, allow_redirects=True) as client:
        resp1 = client.get(f"{_API_BASE}/api/public/file/{token}", headers=headers)
        resp1.raise_for_status()

        resp2 = client.get(f"{_API_BASE}/api/streaming/assign/{token}", headers=headers)
        resp2.raise_for_status()
        data = resp2.json()

        stream_base = data.get("url") if isinstance(data, dict) else None
        stream_token = data.get("token") if isinstance(data, dict) else None
        if not stream_base or not stream_token:
            raise ValueError(EXCEPTIONS["flyf"]["no_stream_url"].format(embed_url=repr(embed_url)))

        candidates = (
            (f"{stream_base}/hls/{stream_token}/master.m3u8", "m3u8"),
            (f"{stream_base}/raw/{stream_token}", "mp4"),
        )
        for candidate, ext in candidates:
            probe = client.head(candidate, headers=headers, allow_redirects=True)
            if probe.status_code < 400:
                return Stream(url=candidate, headers={"Referer": embed_url, "User-Agent": USER_AGENT}, ext=ext)

    raise ValueError(EXCEPTIONS["flyf"]["no_stream_url"].format(embed_url=repr(embed_url)))
