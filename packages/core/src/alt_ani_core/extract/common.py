from urllib.parse import urlsplit

from alt_ani_core.models import Stream as Stream
from alt_ani_core.models import StreamProtocol as StreamProtocol
from alt_ani_core.rules.failure import CATEGORY_NO_STREAM_URL as CATEGORY_NO_STREAM_URL
from alt_ani_core.rules.failure import CATEGORY_PARSER_DRIFT as CATEGORY_PARSER_DRIFT


class ExtractError(ValueError):
    """Stays a ValueError so existing ``except ValueError`` call sites keep working."""

    def __init__(self, message: str, category: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status


def sniff_protocol(url: str) -> StreamProtocol:
    """For hosts whose API returns a bare URL with no media type: decide from the URL path, never the query."""
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        path = url.lower()
    if path.endswith(".m3u8") or "/hls/" in path or "m3u8" in path:
        return StreamProtocol.HLS
    if path.endswith(".mpd"):
        return StreamProtocol.DASH
    return StreamProtocol.PROGRESSIVE


def container_of(url: str, protocol: StreamProtocol, fallback: str = "mp4") -> str | None:
    """Download file extension: None for segmented streams (the downloader remuxes), else by URL path suffix."""
    if protocol is not StreamProtocol.PROGRESSIVE:
        return None
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return fallback
    return next((ext for ext in ("mkv", "mp4", "webm") if path.endswith(f".{ext}")), fallback)


def single_stream(url: str, headers: dict[str, str], protocol: StreamProtocol) -> Stream:
    return Stream.single(url, protocol, headers, container=container_of(url, protocol))
