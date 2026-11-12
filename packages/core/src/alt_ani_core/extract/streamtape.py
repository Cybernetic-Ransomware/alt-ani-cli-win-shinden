import re

from alt_ani_core.config import USER_AGENT
from alt_ani_core.extract.common import (
    CATEGORY_PARSER_DRIFT,
    ExtractError,
    Stream,
    StreamProtocol,
    single_stream,
)
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.transport.api import EXTRACTOR_PROFILE, Transport

# Streamtape concatenates two JS strings to form the /get_video token URL.
# Pattern: robotlink.innerHTML = '/get_video?id=...' + '&expires=...&ip=...'
_ROBOT_RE = re.compile(r"robotlink[\"']?\)?\.innerHTML\s*=\s*[\"']([^\"']+)[\"']\s*\+\s*[\"']([^\"']+)[\"']")
_DIRECT_RE = re.compile(r'"(https?://streamtape\.[a-z]+/get_video[^"]+)"')


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

    m = _ROBOT_RE.search(html)
    if m:
        path = m.group(1) + m.group(2).strip()
        if path.startswith("//"):
            path = "https:" + path
        elif not path.startswith("http"):
            path = "https://streamtape.com" + path
        return single_stream(path, {"Referer": embed_url, "User-Agent": USER_AGENT}, StreamProtocol.PROGRESSIVE)

    m2 = _DIRECT_RE.search(html)
    if m2:
        return single_stream(m2.group(1), {"Referer": embed_url, "User-Agent": USER_AGENT}, StreamProtocol.PROGRESSIVE)

    raise ExtractError(EXCEPTIONS["streamtape"]["no_token"].format(embed_url=repr(embed_url)), CATEGORY_PARSER_DRIFT)
