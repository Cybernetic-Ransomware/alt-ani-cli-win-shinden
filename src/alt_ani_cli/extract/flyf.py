import re
from typing import Any

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream

# api.flyfile.app is a fixed API domain, distinct from the flyf.lat embed host.
_API_BASE = "https://api.flyfile.app"
_EMBED_RE = re.compile(r"^(https?://[^/]+)/embed/([A-Za-z0-9_-]+)")


def _hls_ready(metadata: Any) -> bool:
    """True if the /public/file metadata reports a READY HLS quality tier.

    This call is best-effort — /streaming/assign is the actual required step — so any
    shape mismatch here just means falling back to the raw file instead of aborting.
    """
    if not isinstance(metadata, dict):
        return False
    video_asset = metadata.get("videoAsset")
    if not isinstance(video_asset, dict):
        return False
    qualities = video_asset.get("qualities")
    if not isinstance(qualities, list):
        return False
    return any(isinstance(q, dict) and q.get("status") == "READY" for q in qualities)


def resolve(embed_url: str, referer: str) -> Stream:
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
        hls_ready = False
        try:
            resp1 = client.get(f"{_API_BASE}/api/public/file/{token}", headers=headers)
            resp1.raise_for_status()
            hls_ready = _hls_ready(resp1.json())
        except Exception:
            hls_ready = False

        resp2 = client.get(f"{_API_BASE}/api/streaming/assign/{token}", headers=headers)
        resp2.raise_for_status()
        data = resp2.json()

    stream_base = data.get("url") if isinstance(data, dict) else None
    stream_token = data.get("token") if isinstance(data, dict) else None
    if not stream_base or not stream_token:
        raise ValueError(EXCEPTIONS["flyf"]["no_stream_url"].format(embed_url=repr(embed_url)))

    if hls_ready:
        url, ext = f"{stream_base}/hls/{stream_token}/master.m3u8", "m3u8"
    else:
        url, ext = f"{stream_base}/raw/{stream_token}", "mp4"

    return Stream(url=url, headers={"Referer": embed_url, "User-Agent": USER_AGENT}, ext=ext)
