import time

import httpx
from selectolax.parser import HTMLParser

from alt_ani_cli.config import (
    ANTIBOT_DELAY_SEC,
    GUEST_AUTH,
    SHINDEN_API_BASE,
    SHINDEN_BASE,
)
from alt_ani_cli.content import EXCEPTIONS
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
        raise AntiBotError(EXCEPTIONS["api"]["player_load_403"])
    resp_load.raise_for_status()

    time.sleep(sleep_seconds)

    resp_show = client.get(f"{base}/player_show?{auth}&width=0&height=-1")
    if resp_show.status_code == 403:
        raise AntiBotError(EXCEPTIONS["api"]["player_show_403"])
    resp_show.raise_for_status()

    tree = HTMLParser(resp_show.text)
    iframe = tree.css_first("iframe")
    if not iframe:
        raise AntiBotError(EXCEPTIONS["api"]["no_iframe"].format(online_id=repr(online_id), snippet=repr(resp_show.text[:200])))

    src = iframe.attributes.get("src")
    if not src:
        raise AntiBotError(EXCEPTIONS["api"]["iframe_no_src"].format(online_id=repr(online_id)))
    if src.startswith("//"):
        src = "https:" + src
    elif not src.startswith("http"):
        raise AntiBotError(EXCEPTIONS["api"]["unexpected_iframe_src"].format(src=repr(src), online_id=repr(online_id)))

    return EmbedURL(url=src, referer=f"{SHINDEN_BASE}/")
