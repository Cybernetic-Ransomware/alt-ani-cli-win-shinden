from urllib.parse import parse_qs, urlparse

from alt_ani_core.config import USER_AGENT
from alt_ani_core.extract.common import (
    CATEGORY_NO_STREAM_URL,
    CATEGORY_PARSER_DRIFT,
    ExtractError,
    Stream,
    StreamProtocol,
    container_of,
)
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.models import StreamVariant
from alt_ani_core.transport.api import EXTRACTOR_PROFILE, Transport

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
        raise ExtractError(EXCEPTIONS["lycoris"]["bad_embed_url"].format(embed_url=repr(embed_url)), CATEGORY_PARSER_DRIFT)

    return f"{parsed.scheme}://{parsed.netloc}", anime_id, episode


def resolve(embed_url: str, referer: str, transport: Transport) -> Stream:
    base, anime_id, episode = _embed_params(embed_url)

    with transport.open(EXTRACTOR_PROFILE) as client:
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
        raise ExtractError(EXCEPTIONS["lycoris"]["no_primary_source"].format(embed_url=repr(embed_url)), CATEGORY_NO_STREAM_URL)

    qualities = {
        label: url for key, label in _QUALITY_KEYS.items() if isinstance((url := primary.get(key)), str) and url.strip()
    }

    source_mkv = primary.get("SourceMKV")
    if isinstance(source_mkv, str) and source_mkv.strip():
        qualities.setdefault("source-mkv", source_mkv)

    if not qualities:
        raise ExtractError(EXCEPTIONS["lycoris"]["no_stream_url"].format(embed_url=repr(embed_url)), CATEGORY_NO_STREAM_URL)

    headers = {"Referer": embed_url, "User-Agent": USER_AGENT}
    variants = tuple(
        StreamVariant(
            url=url,
            protocol=StreamProtocol.PROGRESSIVE,
            headers=headers,
            label=label,
            container=container_of(url, StreamProtocol.PROGRESSIVE),
        )
        for label, url in qualities.items()
    )
    preferred = next((i for label in ("1080p", "720p", "480p") for i, v in enumerate(variants) if v.label == label), 0)
    return Stream(variants=variants, default_index=preferred)
