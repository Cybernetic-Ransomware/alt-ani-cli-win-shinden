from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesHit:
    id: str
    slug: str
    title: str
    url: str
    series_type: str = ""
    year: int | None = None


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


@dataclass(frozen=True)
class EmbedURL:
    url: str
    referer: str
