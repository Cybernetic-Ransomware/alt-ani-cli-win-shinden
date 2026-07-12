from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesHit:
    id: str
    slug: str
    title: str
    url: str
    series_type: str = ""


@dataclass(frozen=True)
class SeriesRef:
    id: str
    slug: str
    title: str
    url: str


@dataclass(frozen=True)
class EpisodeRow:
    number: float
    title: str
    url: str


@dataclass(frozen=True)
class PlayerEntry:
    online_id: str
    player: str
    lang_audio: str
    lang_subs: str
    max_res: str | None = None
    date_added: str | None = None
    subs_author: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class PlayerSource:
    online_id: str
    host: str
    embed_url: str


@dataclass(frozen=True)
class EmbedURL:
    url: str
    referer: str


@dataclass(frozen=True)
class RelatedSeries:
    id: str
    slug: str
    title: str
    url: str
    relation: str


@dataclass(frozen=True)
class SeriesMetadata:
    air_date: str | None
    air_date_sort: tuple[int, int, int] | None
    description: str
    tags: tuple[str, ...]
    related: tuple[RelatedSeries, ...]
