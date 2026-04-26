import time

import httpx
from selectolax.parser import HTMLParser

from alt_ani_cli.config import (
    ANTIBOT_DELAY_SEC,
    GUEST_AUTH,
    SHINDEN_API_BASE,
    SHINDEN_BASE,
)
from alt_ani_cli.errors import AntiBotError
from alt_ani_cli.shinden.models import EmbedURL


def resolve_embed(
    client: httpx.Client,
    online_id: str,
    *,
    sleep_seconds: float = ANTIBOT_DELAY_SEC,
) -> EmbedURL:
    """Two-step protocol: player_load → sleep → player_show → iframe src.

    The 5 s sleep is a hard antibot delay enforced server-side.
    Shortening it causes player_show to return an empty response.
    """
    base = f"{SHINDEN_API_BASE}/xhr/{online_id}"
    auth = f"auth={GUEST_AUTH}"

    resp_load = client.get(f"{base}/player_load?{auth}")
    if resp_load.status_code == 403:
        raise AntiBotError(
            "shinden API returned 403 on player_load — "
            "GUEST_AUTH in config.py may have expired."
        )
    resp_load.raise_for_status()

    time.sleep(sleep_seconds)

    resp_show = client.get(f"{base}/player_show?{auth}&width=0&height=-1")
    if resp_show.status_code == 403:
        raise AntiBotError("shinden API returned 403 on player_show.")
    resp_show.raise_for_status()

    tree = HTMLParser(resp_show.text)
    iframe = tree.css_first("iframe")
    if not iframe:
        raise AntiBotError(
            f"player_show returned no iframe for online_id={online_id!r}. "
            f"Response snippet: {resp_show.text[:200]!r}. "
            f"Try setting ALT_ANI_CLI_ANTIBOT_DELAY=7"
        )

    src = iframe.attributes.get("src", "")
    if src.startswith("//"):
        src = "https:" + src
    elif not src.startswith("http"):
        raise AntiBotError(f"Unexpected iframe src={src!r} for online_id={online_id!r}")

    return EmbedURL(url=src, referer=f"{SHINDEN_BASE}/")
