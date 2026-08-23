import re

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream

# api.flyfile.app is a fixed API domain, distinct from the flyf.lat embed host.
_API_BASE = "https://api.flyfile.app"
_EMBED_RE = re.compile(r"^(https?://[^/]+)/embed/([A-Za-z0-9_-]+)")


def resolve(embed_url: str, referer: str) -> Stream:
    """Resolve to the raw file, not HLS.

    A READY status on the /public/file HLS quality tier does not mean the HLS master is
    actually playable — observed live, it produced video with no audio track. The raw
    file is the only variant confirmed to carry both.
    """
    m = _EMBED_RE.match(embed_url)
    if not m:
        raise ValueError(EXCEPTIONS["flyf"]["bad_embed_url"].format(embed_url=repr(embed_url)))
    token = m.group(2)

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": embed_url,
        "X-FlyFile-View": "embed",
        "X-Embed-Referrer": embed_url,
        "X-Adblock-Detected": "0",
    }

    with cffi_requests.Session(impersonate="chrome", timeout=30.0, allow_redirects=True) as client:
        resp = client.get(f"{_API_BASE}/api/streaming/assign/{token}", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    stream_base = data.get("url") if isinstance(data, dict) else None
    stream_token = data.get("token") if isinstance(data, dict) else None
    if not stream_base or not stream_token:
        raise ValueError(EXCEPTIONS["flyf"]["no_stream_url"].format(embed_url=repr(embed_url)))

    url = f"{stream_base}/raw/{stream_token}"
    return Stream(url=url, headers={"Referer": embed_url, "User-Agent": USER_AGENT}, ext="mp4")
