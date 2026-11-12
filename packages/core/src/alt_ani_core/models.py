import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


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
class EpisodeListing:
    ref: SeriesRef
    episodes: list[EpisodeRow]
    pages_fetched: int = 1
    pages_total: int = 1
    # page_limit, http_error, repeated_page, empty_page or count_mismatch; None for a complete listing
    incomplete_reason: str | None = None

    @property
    def complete(self) -> bool:
        return self.incomplete_reason is None


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


class StreamProtocol(StrEnum):
    HLS = "hls"
    DASH = "dash"
    PROGRESSIVE = "progressive"


_HEIGHT_RE = re.compile(r"(\d+)p")


@dataclass(frozen=True)
class StreamVariant:
    """One playable rendition; the URL is short-lived and bound to these headers, so it is never persisted."""

    url: str
    protocol: StreamProtocol
    headers: Mapping[str, str] = field(default_factory=dict)
    label: str = ""
    # file extension a download of this rendition gets; None lets the downloader decide (HLS remux)
    container: str | None = None

    @property
    def height(self) -> int:
        m = _HEIGHT_RE.fullmatch(self.label)
        return int(m.group(1)) if m else 0


@dataclass(frozen=True)
class Stream:
    """What an extractor found: at least one variant, with the extractor's own preference as the default."""

    variants: tuple[StreamVariant, ...]
    default_index: int = 0

    def __post_init__(self) -> None:
        if not self.variants or not 0 <= self.default_index < len(self.variants):
            raise ValueError("a stream needs at least one variant and a default inside it")

    @classmethod
    def single(
        cls, url: str, protocol: StreamProtocol, headers: Mapping[str, str] | None = None, container: str | None = None
    ) -> Stream:
        return cls(variants=(StreamVariant(url=url, protocol=protocol, headers=dict(headers or {}), container=container),))

    @property
    def default(self) -> StreamVariant:
        return self.variants[self.default_index]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(v.label for v in self.variants if v.label)

    def select(self, quality: str) -> StreamVariant:
        """``best``/``worst`` by height among labelled variants, an exact label, else the default."""
        labelled = [v for v in self.variants if v.label]
        if not labelled:
            return self.default
        if quality == "best":
            return max(labelled, key=lambda v: v.height)
        if quality == "worst":
            return min(labelled, key=lambda v: v.height)
        return next((v for v in labelled if v.label == quality), self.default)


@dataclass(frozen=True)
class ExtractionFailure:
    """Why extraction failed, by layer; classified primitives only, never a URL or message."""

    layer: str
    category: str
    http_status: int | None = None
    used_fallback: bool = False
    fallback_layer: str | None = None
    fallback_category: str | None = None
    fallback_http_status: int | None = None


@dataclass(frozen=True)
class SourceFilter:
    lang: str | None = None
    subs: str | None = None
    player_name: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.lang or self.subs or self.player_name)


class Purpose(StrEnum):
    WATCH = "watch"
    DOWNLOAD = "download"


@dataclass(frozen=True)
class SourceSelection:
    """``unmatched`` non-empty means the filters matched nothing; using ``all_sources`` is the caller's explicit choice."""

    candidates: tuple[PlayerEntry, ...]
    unmatched: tuple[str, ...]
    all_sources: tuple[PlayerEntry, ...]


class FallbackPolicy(StrEnum):
    # only the chosen source, never deferred or reordered: a person picked it
    SINGLE = "single"
    # chosen source first, then the rest; hosts known unavailable are deferred and get one last-resort attempt
    WALK = "walk"


@dataclass(frozen=True)
class ResolvedStream:
    stream: Stream
    source: PlayerEntry
    embed: EmbedURL


@dataclass(frozen=True)
class SourceAttempt:
    source: PlayerEntry
    host: str | None
    error: str
