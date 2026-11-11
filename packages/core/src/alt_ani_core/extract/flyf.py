import re

from alt_ani_core.config import USER_AGENT
from alt_ani_core.extract.common import (
    CATEGORY_NO_STREAM_URL,
    CATEGORY_PARSER_DRIFT,
    ExtractError,
    Stream,
    StreamProtocol,
    single_stream,
)
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.transport.api import EXTRACTOR_PROFILE, Transport

# api.flyfile.app is a fixed API domain, distinct from the flyf.lat embed host.
_API_BASE = "https://api.flyfile.app"
_EMBED_RE = re.compile(r"^(https?://[^/]+)/embed/([A-Za-z0-9_-]+)")


def resolve(embed_url: str, referer: str, transport: Transport) -> Stream:
    """Resolve to the raw file, not HLS.

    A READY status on the /public/file HLS quality tier does not mean the HLS master is
    actually playable — observed live, it produced video with no audio track. The raw
    file is the only variant confirmed to carry both.
    """
    m = _EMBED_RE.match(embed_url)
    if not m:
        raise ExtractError(EXCEPTIONS["flyf"]["bad_embed_url"].format(embed_url=repr(embed_url)), CATEGORY_PARSER_DRIFT)
    token = m.group(2)

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": embed_url,
        "X-FlyFile-View": "embed",
        "X-Embed-Referrer": embed_url,
        "X-Adblock-Detected": "0",
    }

    with transport.open(EXTRACTOR_PROFILE) as client:
        resp = client.get(f"{_API_BASE}/api/streaming/assign/{token}", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    stream_base = data.get("url") if isinstance(data, dict) else None
    stream_token = data.get("token") if isinstance(data, dict) else None
    if not stream_base or not stream_token:
        raise ExtractError(EXCEPTIONS["flyf"]["no_stream_url"].format(embed_url=repr(embed_url)), CATEGORY_NO_STREAM_URL)

    url = f"{stream_base}/raw/{stream_token}"
    return single_stream(url, {"Referer": embed_url, "User-Agent": USER_AGENT}, StreamProtocol.PROGRESSIVE)
