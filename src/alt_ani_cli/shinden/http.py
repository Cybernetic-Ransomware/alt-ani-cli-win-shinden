import time

import httpx

from alt_ani_cli.config import SHINDEN_BASE, USER_AGENT


def make_client() -> httpx.Client:
    """Build an httpx.Client with the full set of headers shinden.pl expects.

    The _rnd cookie is a Unix timestamp — shinden uses it as a lightweight
    session seed. It must be present from the very first request and persisted
    across the warmup → player_show call pair.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": f"{SHINDEN_BASE}/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "sec-gpc": "1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
    cookies = {"_rnd": str(int(time.time()))}
    return httpx.Client(
        headers=headers,
        cookies=cookies,
        follow_redirects=True,
        timeout=30.0,
    )
