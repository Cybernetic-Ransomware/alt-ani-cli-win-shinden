"""History contract: a per-series resume cursor for watching and a set of obtained episodes for downloads."""

from dataclasses import dataclass
from typing import Protocol

from alt_ani_core.models import SeriesRef


@dataclass(frozen=True)
class WatchEntry:
    """``last_ep`` is where to resume from, as reported by the application — not proof the episode was watched."""

    series: SeriesRef
    last_ep: float
    updated_at: str


@dataclass(frozen=True)
class DownloadEntry:
    series: SeriesRef
    episodes: frozenset[float]
    updated_at: str


@dataclass(frozen=True)
class LegacySeries:
    """One series of the pre-SQLite JSON history, with its identifiers and timestamps kept verbatim."""

    series: SeriesRef
    last_ep: float | None = None
    watch_updated_at: str | None = None
    downloaded: tuple[float, ...] = ()
    download_updated_at: str | None = None


class HistoryStore(Protocol):
    def record_watch(self, series: SeriesRef, episode: float) -> None: ...

    def record_download(self, series: SeriesRef, episode: float) -> None: ...

    def watch_entries(self) -> list[WatchEntry]:
        """Newest first."""
        ...

    def download_entries(self) -> list[DownloadEntry]:
        """Newest first."""
        ...

    def clear(self) -> None: ...
