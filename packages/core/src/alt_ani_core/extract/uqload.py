import re

from alt_ani_core.config import USER_AGENT
from alt_ani_core.extract.common import (
    CATEGORY_PARSER_DRIFT,
    ExtractError,
    Stream,
    single_stream,
    sniff_protocol,
)
from alt_ani_core.extract.jwplayer import unpack_packer
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.transport.api import EXTRACTOR_PROFILE, Transport

_EMBED_RE = re.compile(r"^(https?://[^/]+)/e/([A-Za-z0-9_-]+)")
_FILE_RE = re.compile(r'file\s*:\s*"([^"]+)"')


def resolve(embed_url: str, referer: str, transport: Transport) -> Stream:
    m = _EMBED_RE.match(embed_url)
    if not m:
        raise ExtractError(EXCEPTIONS["uqload"]["bad_embed_url"].format(embed_url=repr(embed_url)), CATEGORY_PARSER_DRIFT)
    base, file_code = m.group(1), m.group(2)

    with transport.open(EXTRACTOR_PROFILE) as client:
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
        raise ExtractError(EXCEPTIONS["uqload"]["no_video_url"].format(embed_url=repr(embed_url)), CATEGORY_PARSER_DRIFT)

    url = m2.group(1)
    return single_stream(url, {"Referer": embed_url, "User-Agent": USER_AGENT}, sniff_protocol(url))
