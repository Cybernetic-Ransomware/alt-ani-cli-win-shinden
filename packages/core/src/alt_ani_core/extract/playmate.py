import re

from alt_ani_core.config import USER_AGENT
from alt_ani_core.extract.common import (
    CATEGORY_NO_STREAM_URL,
    CATEGORY_PARSER_DRIFT,
    ExtractError,
    Stream,
    single_stream,
    sniff_protocol,
)
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.transport.api import EXTRACTOR_PROFILE, Transport

_EMBED_RE = re.compile(r"^(https?://[^/]+)/embed/([A-Za-z0-9_-]+)")


def resolve(embed_url: str, referer: str, transport: Transport) -> Stream:
    m = _EMBED_RE.match(embed_url)
    if not m:
        raise ExtractError(EXCEPTIONS["playmate"]["bad_embed_url"].format(embed_url=repr(embed_url)), CATEGORY_PARSER_DRIFT)
    base, filecode = m.group(1), m.group(2)

    with transport.open(EXTRACTOR_PROFILE) as client:
        resp = client.post(
            f"{base}/api/s",
            json={"c": filecode, "d": "web"},
            headers={
                "Origin": base,
                "Referer": embed_url,
                "User-Agent": USER_AGENT,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    url = data.get("sx") if isinstance(data, dict) else None
    if not url:
        raise ExtractError(EXCEPTIONS["playmate"]["no_stream_url"].format(embed_url=repr(embed_url)), CATEGORY_NO_STREAM_URL)

    return single_stream(url, {"Referer": embed_url, "Origin": base, "User-Agent": USER_AGENT}, sniff_protocol(url))
