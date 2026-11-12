import threading
from dataclasses import dataclass

import pytest

from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import StorageError
from alt_ani_core.history.database import Database
from alt_ani_core.history.queue import SqliteDownloadQueue
from alt_ani_core.services.offline.library import ResultKind
from alt_ani_core.services.offline.sources import EngineSources
from alt_ani_core.state import (
    DownloadCriteria,
    ItemStatus,
    JobState,
    NetworkKind,
    OpenedVia,
    ResourceValidator,
)
from alt_ani_core.storage import fs
from alt_ani_core.transfer import TransferResult

from .servers import Resource, ServerState, random_body, serve
from .support import SERIES, FakeSources, episode, make_queue, player, progressive, wait_for

pytestmark = pytest.mark.loopback


def _library_with(tmp_path, numbers=(1, 2)):
    body = random_body(100_000)
    server = ServerState(resources={"/v.mp4": Resource(body, '"e1"')})
    with serve(server) as url:
        sources = FakeSources([player("1")], {"1": lambda _f: progressive(f"{url}/v.mp4")})
        queue, state, _ = make_queue(tmp_path, sources)
        jobs = queue.enqueue(SERIES, [episode(n) for n in numbers], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: all(state.queue.get(j.id).state is JobState.COMPLETED for j in jobs))
        finally:
            queue.close()
    items = [state.library.get(state.queue.get(j.id).offline_item_id) for j in jobs]
    return queue, state, items, body


@pytest.mark.unit
class TestLibraryService:
    def test_delete_removes_file_and_row_but_keeps_history(self, tmp_path):
        queue, state, (item, _), _ = _library_with(tmp_path)
        state.watch.record_opened(SERIES, episode(1), OpenedVia.PLAYER)
        (result,) = queue.delete([item.id])
        assert result.kind is ResultKind.OK
        assert not (tmp_path / "library" / item.spec.locator.key).exists()
        assert state.watch.get(item.key).opened_via is OpenedVia.PLAYER
        assert state.queue.get(state.queue.jobs()[0].id).state is JobState.COMPLETED

    def test_failed_removal_keeps_the_row_for_a_retry(self, tmp_path, monkeypatch):
        queue, state, (item, _), _ = _library_with(tmp_path)

        def denied(self, locator):
            raise StorageError("storage.access_denied").coded("storage.access_denied")

        real = fs.FsFileStore.remove
        monkeypatch.setattr(fs.FsFileStore, "remove", denied)
        (result,) = queue.delete([item.id])
        assert (result.kind, result.code) == (ResultKind.FAILED, "storage.access_denied")
        assert state.library.get(item.id).status is ItemStatus.DELETING
        monkeypatch.setattr(fs.FsFileStore, "remove", real)
        (retried,) = queue.library.retry_pending_deletes()
        assert retried.kind is ResultKind.OK and state.library.items(status=ItemStatus.DELETING) == []

    def test_bulk_delete_reports_each_item(self, tmp_path):
        queue, state, (first, second), _ = _library_with(tmp_path)
        state.library.mark_missing(second.id)
        (job,) = state.queue.enqueue(SERIES, [episode(2)], DownloadCriteria()).added
        from alt_ani_core.history.ownership import ExecutorOwnership

        owner = ExecutorOwnership.try_acquire(tmp_path / "history.db")
        claim = state.queue.claim_next(NetworkKind.WIFI, owner)
        state.queue.set_target(claim.attempt_id, second.spec.locator)
        owner.release()
        results = queue.delete([first.id, "nope", second.id])
        assert [(r.kind, r.code) for r in results] == [
            (ResultKind.OK, None),
            (ResultKind.FAILED, "state.unknown_item"),
            (ResultKind.FAILED, "state.locator_reserved"),
        ]

    def test_export_is_an_independent_copy_and_conflicts_get_a_number(self, tmp_path):
        queue, state, (item, _), body = _library_with(tmp_path)
        out = tmp_path / "exported"
        (first,) = queue.export([item.id], out)
        (second,) = queue.export([item.id], out)
        assert first.path.read_bytes() == body and second.path.name.endswith(" (2).mp4")
        queue.delete([item.id])
        assert first.path.read_bytes() == body
        assert sorted(p.name for p in out.iterdir()) == sorted([first.path.name, second.path.name])

    def test_export_skips_unavailable_items(self, tmp_path):
        queue, state, (item, other), _ = _library_with(tmp_path)
        state.library.mark_missing(item.id)
        results = queue.export([item.id, other.id], tmp_path / "out")
        assert [r.kind for r in results] == [ResultKind.SKIPPED, ResultKind.OK]


def _counting(monkeypatch, cls, name):
    calls = []
    real = getattr(cls, name)

    def wrapper(self, *args, **kwargs):
        calls.append(name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, wrapper)
    return calls


class BurstTransfer:
    """Reports progress as fast as possible with a fake clock; stops at the first callback after a cancel."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.callbacks_after_stop = None
        self.paused = threading.Event()

    def __call__(self, variant, work, *, resume, callbacks, policy, events, cancel, socket_timeout):
        callbacks.validator(ResourceValidator("http", (("etag", "e" * 64), ("length", 0), ("total", 10**9))))
        for i in range(10_000):
            self.clock.now += 0.002
            callbacks.progress(i * 1000, 10**9)
            if i == 5_000:
                self.paused.set()
            if cancel.cancelled:
                self.callbacks_after_stop = i
                cancel.raise_if_cancelled()
            threading.Event().wait(0.0001)
        media = work / "media.mp4"
        media.write_bytes(b"x")
        return TransferResult(media=media, size=1, protocol="http", extension="mp4")


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.unit
class TestWriteRates:
    def test_progress_writes_are_throttled_without_delaying_a_stop(self, tmp_path, monkeypatch):
        from .support import fast_config

        clock = FakeClock()
        burst = BurstTransfer(clock)
        writes = _counting(monkeypatch, SqliteDownloadQueue, "report_progress")
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, _ = make_queue(
            tmp_path, sources, transfer=burst, clock=clock, config=fast_config(progress_interval_sec=2.0)
        )
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            assert burst.paused.wait(30)
            queue.pause(job.id)
            wait_for(lambda: state.queue.get(job.id).state is JobState.PAUSED)
        finally:
            queue.close()
        # 10 000 callbacks over 20 fake seconds: one write per 2 s, however soon the stop came
        assert len(writes) <= 11
        assert burst.callbacks_after_stop is not None and burst.callbacks_after_stop < 10_000

    def test_idle_scheduler_neither_writes_nor_spins(self, tmp_path, monkeypatch):
        queue, state, _ = make_queue(tmp_path, FakeSources([], {}))
        queue.start()
        try:
            threading.Event().wait(0.3)
            writes = _counting(monkeypatch, Database, "write")
            ticks_before = queue._scheduler.ticks
            threading.Event().wait(1.0)
            ticks = queue._scheduler.ticks - ticks_before
        finally:
            queue.close()
        assert writes == []
        assert ticks <= 7


@pytest.mark.unit
class TestExpiredUrl:
    def test_expired_stream_url_is_resolved_again_and_the_partial_resumed(self, tmp_path):
        body = random_body(3_000_000)
        server = ServerState(resources={"/v.mp4": Resource(body, '"e1"')}, stall_after=1_000_000)
        with serve(server) as url:

            def stream(fresh: bool):
                return progressive(f"{url}/v.mp4?t={'new' if fresh else 'old'}")

            sources = FakeSources([player("1")], {"1": stream})
            queue, state, _ = make_queue(tmp_path, sources)
            (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
            queue.start()
            try:
                wait_for(
                    lambda: (
                        (p := state.queue.get(job.id).partial) and p.validator and dict(p.validator.fields).get("length", 0) > 0
                    )
                )
                server.forbidden_query = "t=old"
                server.stall_after = None
                server.stall.set()
                wait_for(lambda: state.queue.get(job.id).state is JobState.COMPLETED, timeout=30)
            finally:
                queue.close()
        assert ("1", True) in sources.episode.resolved
        item = state.library.get(state.queue.get(job.id).offline_item_id)
        assert (tmp_path / "library" / item.spec.locator.key).read_bytes() == body


class _FakeEngine:
    def __init__(self) -> None:
        self.access: list[AccessConfig] = []

    def set_access(self, access: AccessConfig) -> None:
        self.access.append(access)


@pytest.mark.unit
class TestFlareSolverrFollowsTheNetwork:
    @pytest.mark.parametrize(("configured", "expected"), [(True, [True, False, True]), (False, [False])])
    def test_solver_only_on_wifi_and_only_when_configured(self, configured, expected):
        engine = _FakeEngine()
        sources = EngineSources(engine, AccessConfig(flaresolverr_url="http://solver", use_flaresolverr=configured))
        for network in (NetworkKind.WIFI, NetworkKind.CELLULAR, NetworkKind.CELLULAR, NetworkKind.WIFI):
            sources.network_changed(network)
        assert [a.use_flaresolverr for a in engine.access] == expected
