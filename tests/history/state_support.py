import itertools
import sqlite3
from contextlib import closing
from pathlib import Path

from alt_ani_core.history.local import LocalState, open_local_state
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.models import EpisodeRow, SeriesRef
from alt_ani_core.state import (
    DownloadCriteria,
    EpisodeKey,
    OfflineItemSpec,
    PartialTransfer,
    ResourceValidator,
    SourceIdentity,
    StorageLocator,
    VariantIdentity,
)

FIXTURES = Path(__file__).parent / "fixtures"

SOURCE = SourceIdentity(
    online_id="777", player="host", lang_audio="jp", lang_subs="pl", max_res="1080p", subs_author="", source=""
)
OTHER_SOURCE = SourceIdentity(
    online_id="778", player="other", lang_audio="jp", lang_subs="pl", max_res="720p", subs_author="", source=""
)
VARIANT = VariantIdentity(label="720p", height=720, protocol="hls")
VALIDATOR = ResourceValidator("hls", (("segments_sha256", "ab12"), ("segments", 300)))


def ref(series_id: str = "1", title: str = "Fate") -> SeriesRef:
    return SeriesRef(id=series_id, slug="fate", title=title, url=f"https://shinden.pl/series/{series_id}-fate")


def row(number: float) -> EpisodeRow:
    return EpisodeRow(number=number, title=f"Episode {number:g}", url=f"https://shinden.pl/episode/1-fate/view/{number:g}")


def rows(*numbers: float) -> list[EpisodeRow]:
    return [row(n) for n in numbers]


def key(number: float, series_id: str = "1") -> EpisodeKey:
    return EpisodeKey(series_id, number)


def ticking_clock():
    """Strictly increasing timestamps, so ordering never depends on how fast two writes follow each other."""
    ticks = itertools.count(1)

    def now() -> str:
        tick = next(ticks)
        return f"2026-01-01T{tick // 3600:02d}:{tick // 60 % 60:02d}:{tick % 60:02d}+00:00"

    return now


def open_state(path: Path) -> LocalState:
    return open_local_state(path, clock=ticking_clock())


def partial(validator: ResourceValidator | None = VALIDATOR, source: SourceIdentity = SOURCE) -> PartialTransfer:
    return PartialTransfer(StorageLocator("fs", "/videos/fate - ep1.part"), source, VARIANT, validator)


def spec(name: str = "fate - ep1.mp4", backend: str = "fs") -> OfflineItemSpec:
    return OfflineItemSpec(
        locator=StorageLocator(backend, name),
        size_bytes=1000,
        audio="jp",
        subs="pl",
        height=720,
        variant_label="720p",
        sha256=sha(),
    )


def enqueue(state: LocalState, *numbers: float, **kwargs):
    return state.queue.enqueue(ref(), rows(*numbers), kwargs.pop("criteria", DownloadCriteria()), **kwargs)


def download(state: LocalState, number: float = 1, name: str | None = None):
    """Runs one job through claim, finalize and complete, the way the stage-2 executor will."""
    from alt_ani_core.state import NetworkKind

    (job,) = enqueue(state, number).added
    claim = state.queue.claim_next(NetworkKind.WIFI, owner(state))
    assert claim is not None and claim.job.id == job.id
    assert state.queue.begin_finalize(claim.attempt_id)
    item = state.queue.complete(claim.attempt_id, spec(name or f"fate - ep{number:g}.mp4"))
    assert item is not None
    return state.queue.get(job.id), item


_owners: dict[Path, ExecutorOwnership] = {}


def owner(state: LocalState) -> ExecutorOwnership:
    """The executor ownership of this test's database, acquired once and released after the test."""
    path = state.database.path.resolve()
    if path not in _owners:
        acquired = ExecutorOwnership.try_acquire(path)
        assert acquired is not None
        _owners[path] = acquired
    return _owners[path]


def release_owners() -> None:
    while _owners:
        _owners.popitem()[1].release()


def sha(seed: str = "a") -> str:
    return (seed * 64)[:64]


def scalar(path: Path, sql: str):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchone()[0]


def dump_database(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return "\n".join(conn.iterdump())
