"""Plain-data views of engine objects; the XHR token, HTTP sessions and health state never appear here."""

from typing import Any

from alt_ani_core.diagnostics import host_of_url
from alt_ani_core.history.store import DownloadEntry, WatchEntry
from alt_ani_core.models import (
    EpisodeListing,
    EpisodeRow,
    PlayerEntry,
    PlayerSource,
    RelatedSeries,
    ResolvedStream,
    SeriesHit,
    SeriesMetadata,
    SeriesRef,
    SourceSelection,
    StreamProtocol,
    StreamVariant,
)

_MIME = {StreamProtocol.HLS: "application/x-mpegURL", StreamProtocol.DASH: "application/dash+xml"}
_PROGRESSIVE_MIME = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska"}


def series_hit(hit: SeriesHit) -> dict[str, Any]:
    return {"id": hit.id, "slug": hit.slug, "title": hit.title, "url": hit.url, "series_type": hit.series_type}


def series_ref(ref: SeriesRef) -> dict[str, Any]:
    return {"id": ref.id, "slug": ref.slug, "title": ref.title, "url": ref.url}


def parse_series_ref(data: dict[str, Any]) -> SeriesRef:
    return SeriesRef(id=str(data["id"]), slug=str(data.get("slug", "")), title=str(data["title"]), url=str(data["url"]))


def _related(item: RelatedSeries) -> dict[str, Any]:
    return {"id": item.id, "slug": item.slug, "title": item.title, "url": item.url, "relation": item.relation}


def series_metadata(meta: SeriesMetadata | None) -> dict[str, Any] | None:
    if meta is None:
        return None
    return {
        "air_date": meta.air_date,
        "air_date_sort": list(meta.air_date_sort) if meta.air_date_sort else None,
        "description": meta.description,
        "tags": list(meta.tags),
        "related": [_related(item) for item in meta.related],
    }


def episode(row: EpisodeRow) -> dict[str, Any]:
    return {"number": row.number, "title": row.title, "url": row.url}


def parse_episode(data: dict[str, Any]) -> EpisodeRow:
    return EpisodeRow(number=float(data["number"]), title=str(data.get("title", "")), url=str(data["url"]))


def episode_listing(listing: EpisodeListing) -> dict[str, Any]:
    return {
        "series": series_ref(listing.ref),
        "episodes": [episode(row) for row in listing.episodes],
        "complete": listing.complete,
        "incomplete_reason": listing.incomplete_reason,
        "pages_fetched": listing.pages_fetched,
        "pages_total": listing.pages_total,
    }


def player(entry: PlayerEntry) -> dict[str, Any]:
    return {
        "online_id": entry.online_id,
        "player": entry.player,
        "lang_audio": entry.lang_audio,
        "lang_subs": entry.lang_subs,
        "max_res": entry.max_res,
        "date_added": entry.date_added,
        "subs_author": entry.subs_author,
        "source": entry.source,
    }


def selection(value: SourceSelection) -> dict[str, Any]:
    return {
        "candidates": [player(p) for p in value.candidates],
        "unmatched": list(value.unmatched),
        "all_sources": [player(p) for p in value.all_sources],
    }


def known_source(source: PlayerSource) -> dict[str, Any]:
    # the embed URL can carry a per-view token, so only the host crosses the bridge
    return {"online_id": source.online_id, "host": source.host}


def _mime(variant: StreamVariant) -> str | None:
    if variant.protocol is StreamProtocol.PROGRESSIVE:
        return _PROGRESSIVE_MIME.get(variant.container or "")
    return _MIME[variant.protocol]


def _variant(variant: StreamVariant) -> dict[str, Any]:
    return {
        "label": variant.label,
        "url": variant.url,
        "protocol": variant.protocol.value,
        "mime": _mime(variant),
        "container": variant.container,
        "height": variant.height or None,
        "headers": dict(variant.headers),
    }


def resolved_stream(resolved: ResolvedStream, quality: str | None) -> dict[str, Any]:
    stream = resolved.stream
    chosen = stream.select(quality) if quality else stream.default
    return {
        "source": player(resolved.source),
        "embed_host": host_of_url(resolved.embed.url),
        "variants": [_variant(v) for v in stream.variants],
        "default_index": stream.default_index,
        "selected_index": stream.variants.index(chosen),
    }


def watch_entry(entry: WatchEntry) -> dict[str, Any]:
    return {"series": series_ref(entry.series), "last_ep": entry.last_ep, "updated_at": entry.updated_at}


def download_entry(entry: DownloadEntry) -> dict[str, Any]:
    return {"series": series_ref(entry.series), "episodes": sorted(entry.episodes), "updated_at": entry.updated_at}
