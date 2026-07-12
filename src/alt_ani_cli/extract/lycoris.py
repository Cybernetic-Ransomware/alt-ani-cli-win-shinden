from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream

_QUALITY_KEYS = {
    "FHD": "1080p",
    "HD": "720p",
    "SD": "480p",
}


def _embed_params(embed_url: str) -> tuple[str, str, str]:
    parsed = urlparse(embed_url)
    qs = parse_qs(parsed.query)

    anime_id = (qs.get("id") or [""])[0].strip()
    episode = (qs.get("episode") or [""])[0].strip()
    if not parsed.scheme or not parsed.netloc or not anime_id or not episode:
        raise ValueError(EXCEPTIONS["lycoris"]["bad_embed_url"].format(embed_url=repr(embed_url)))

    return f"{parsed.scheme}://{parsed.netloc}", anime_id, episode


def resolve(embed_url: str, referer: str) -> Stream:
    base, anime_id, episode = _embed_params(embed_url)

    with cffi_requests.Session(impersonate="chrome", timeout=30.0, allow_redirects=True) as client:
        resp = client.get(
            f"{base}/api/embed",
            params={"id": anime_id, "episode": episode},
            headers={
                "Referer": embed_url,
                "User-Agent": USER_AGENT,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    episode_info = data.get("episodeInfo") if isinstance(data, dict) else None
    primary = episode_info.get("primarySource") if isinstance(episode_info, dict) else None
    if not isinstance(primary, dict):
        raise ValueError(EXCEPTIONS["lycoris"]["no_primary_source"].format(embed_url=repr(embed_url)))

    qualities = {
        label: url
        for key, label in _QUALITY_KEYS.items()
        if isinstance((url := primary.get(key)), str) and url.strip()
    }

    source_mkv = primary.get("SourceMKV")
    if isinstance(source_mkv, str) and source_mkv.strip():
        qualities.setdefault("source-mkv", source_mkv)

    if not qualities:
        raise ValueError(EXCEPTIONS["lycoris"]["no_stream_url"].format(embed_url=repr(embed_url)))

    best = qualities.get("1080p") or qualities.get("720p") or qualities.get("480p") or next(iter(qualities.values()))
    ext = "mkv" if best.lower().split("?", 1)[0].endswith(".mkv") else "mp4"

    return Stream(
        url=best,
        headers={
            "Referer": embed_url,
            "User-Agent": USER_AGENT,
        },
        qualities=qualities,
        ext=ext,
    )
