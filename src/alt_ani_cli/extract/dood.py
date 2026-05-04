import re
import time

import httpx

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream

_PASS_MD5_RE = re.compile(r"/pass_md5/([a-zA-Z0-9/_-]+)")
_TOKEN_RE = re.compile(r"token=([a-zA-Z0-9]+)")


def resolve(embed_url: str, referer: str) -> Stream:
    m_base = re.match(r"(https?://[^/]+)", embed_url)
    if not m_base:
        raise ValueError(EXCEPTIONS["dood"]["bad_base_url"].format(embed_url=repr(embed_url)))
    base = m_base.group(1)

    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        resp = client.get(embed_url, headers={"Referer": referer, "User-Agent": USER_AGENT})
        resp.raise_for_status()
        html = resp.text

        m_pass = _PASS_MD5_RE.search(html)
        if not m_pass:
            raise ValueError(EXCEPTIONS["dood"]["no_pass_md5"].format(embed_url=repr(embed_url)))

        pass_url = f"{base}/pass_md5/{m_pass.group(1)}"
        resp2 = client.get(
            pass_url,
            headers={
                "Referer": embed_url,
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        resp2.raise_for_status()
        token_base = resp2.text.strip()

    m_token = _TOKEN_RE.search(html)
    token = m_token.group(1) if m_token else ""
    ts = str(int(time.time() * 1000))
    video_url = f"{token_base}?token={token}&expiry={ts}" if token else token_base

    return Stream(
        url=video_url,
        headers={"Referer": base + "/", "User-Agent": USER_AGENT},
        ext="mp4",
    )
