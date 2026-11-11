import base64
import re

from alt_ani_core.config import USER_AGENT
from alt_ani_core.extract.common import (
    CATEGORY_PARSER_DRIFT,
    ExtractError,
    Stream,
    single_stream,
    sniff_protocol,
)
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.transport.api import EXTRACTOR_PROFILE, Transport

_FILE_RE = re.compile(r'["\']?(?:file|src)["\']?\s*:\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']')
_PLAYER_SRC_RE = re.compile(r'player\.src\s*\(\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']')


def resolve(embed_url: str, referer: str, transport: Transport) -> Stream:
    with transport.open(EXTRACTOR_PROFILE) as client:
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
        return single_stream(m.group(1), {"Referer": embed_url, "User-Agent": USER_AGENT}, sniff_protocol(m.group(1)))

    # Some mp4upload variants base64-encode the URL
    b64_match = re.search(r'"file"\s*:\s*"([A-Za-z0-9+/=]{30,})"', html)
    if b64_match:
        try:
            decoded = base64.b64decode(b64_match.group(1)).decode("utf-8")
            if decoded.startswith("http"):
                return single_stream(decoded, {"Referer": embed_url, "User-Agent": USER_AGENT}, sniff_protocol(decoded))
        except Exception:  # nosec B110
            pass

    raise ExtractError(EXCEPTIONS["mp4upload"]["no_video_url"].format(embed_url=repr(embed_url)), CATEGORY_PARSER_DRIFT)
