"""Store outages, shutdown, concurrent deletes and the condition bookkeeping between scheduler passes."""

import sqlite3
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.history.local import open_local_state
from alt_ani_core.history.ownership import DELETE_LOCK_SUFFIX, ExecutorOwnership, lock_path
from alt_ani_core.history.preferences import SqlitePreferencesStore
from alt_ani_core.history.queue import SqliteDownloadQueue
from alt_ani_core.services.offline.attempt import Context
from alt_ani_core.services.offline.events import StoreUnavailable
from alt_ani_core.services.offline.interaction import Grants
from alt_ani_core.services.offline.library import LibraryService, ResultKind
from alt_ani_core.services.offline.network import StaticNetwork
from alt_ani_core.services.offline.scheduler import Scheduler
from alt_ani_core.state import DownloadCriteria, ItemStatus, JobState, NetworkKind, WaitReason
from alt_ani_core.storage.fs import FsFileStore
from alt_ani_core.testing import RecordingSink

from .support import SERIES, FakeSources, GatedTransfer, episode, fast_config, make_queue, player, progressive, wait_for
from .test_recovery import BODY, Crashed


def _gated_sources() -> FakeSources:
    return FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})


def _failing(monkeypatch, cls, name: str) -> threading.Event:
    """While the returned event is set, the method raises the way a locked or broken database file does."""
    failing = threading.Event()
    real = getattr(cls, name)

    def wrapper(self, *args, **kwargs):
        if failing.is_set():
            raise sqlite3.OperationalError("database is locked")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, wrapper)
    return failing


def _available_item(tmp_path):
    crash = Crashed(tmp_path)
    job, _ = crash.finalizing()
    crash.recover()
    return crash, crash.state.library.get(crash.state.queue.get(job.id).offline_item_id)


@pytest.mark.unit
class TestConcurrentDeletes:
    def test_a_second_deleter_never_removes_a_file_published_after_the_first_finished(self, tmp_path):
        crash, item = _available_item(tmp_path)
        (deleting,) = crash.state.library.mark_deleting([item.id])
        target = crash.target_path()
        new_copy = b"newly-published" * 1000
        first_entered, second_checked, published = threading.Event(), threading.Event(), threading.Event()

        first_store = FsFileStore(tmp_path / "library")
        real_digest = first_store.digest

        def slow_digest(locator, cancel):
            first_entered.set()
            second_checked.wait(1.0)
            return real_digest(locator, cancel)

        first_store.digest = slow_digest
        first_state = open_local_state(tmp_path / "history.db")
        real_forget = first_state.library.forget

        def forget_then_republish(item_id):
            real_forget(item_id)
            # the locator is free again: a new download may publish its own copy right now
            target.write_bytes(new_copy)
            published.set()

        first_state.library.forget = forget_then_republish

        second_store = FsFileStore(tmp_path / "library")
        real_second_digest, real_remove = second_store.digest, second_store.remove

        def checked_digest(locator, cancel):
            digest = real_second_digest(locator, cancel)
            second_checked.set()
            return digest

        def late_remove(locator):
            published.wait(5)
            real_remove(locator)

        second_store.digest, second_store.remove = checked_digest, late_remove
        first = LibraryService(first_state.library, first_store)
        second = LibraryService(open_local_state(tmp_path / "history.db").library, second_store)
        results = {}
        runner = threading.Thread(target=lambda: results.setdefault("first", first.finish_delete(deleting)))
        runner.start()
        assert first_entered.wait(10)
        results["second"] = second.finish_delete(deleting)
        runner.join(10)
        assert results["first"].kind is ResultKind.OK
        assert (results["second"].kind, results["second"].code) == (ResultKind.SKIPPED, "not_deleting")
        assert target.read_bytes() == new_copy

    def test_a_delete_in_another_process_excludes_this_one(self, tmp_path):
        crash, item = _available_item(tmp_path)
        (deleting,) = crash.state.library.mark_deleting([item.id])
        marker = tmp_path / "held"
        script = textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from alt_ani_core.history.ownership import exclusive
            with exclusive(Path(sys.argv[1])):
                Path(sys.argv[2]).write_text("held")
                time.sleep(120)
            """
        )
        lock = lock_path(tmp_path / "history.db", DELETE_LOCK_SUFFIX)
        child = subprocess.Popen([sys.executable, "-c", script, str(lock), str(marker)])
        try:
            wait_for(lambda: marker.exists() and marker.read_text() == "held", timeout=30)
            cancel = CancelToken()
            threading.Timer(0.3, cancel.cancel).start()
            blocked = crash_service(crash).finish_delete(deleting, cancel=cancel)
            assert (blocked.kind, blocked.code) == (ResultKind.FAILED, "cancelled")
            assert crash.target_path().exists()
            assert crash.state.library.get(item.id).status is ItemStatus.DELETING
        finally:
            child.kill()
            child.wait(timeout=30)
        assert crash_service(crash).finish_delete(deleting).kind is ResultKind.OK
        assert not crash.target_path().exists()


def crash_service(crash: Crashed) -> LibraryService:
    return LibraryService(crash.state.library, crash.store)


@pytest.mark.unit
class TestDeepVerify:
    def test_same_length_other_content_is_caught_only_by_a_deep_check(self, tmp_path):
        crash, item = _available_item(tmp_path)
        crash.target_path().write_bytes(b"x" * len(BODY))
        service = crash_service(crash)
        (shallow,) = service.verify([item.id])
        assert shallow.kind is ResultKind.OK
        (deep,) = service.verify([item.id], deep=True)
        assert (deep.kind, deep.code) == (ResultKind.FAILED, "identity_mismatch")
        assert crash.state.library.get(item.id).status is ItemStatus.MISSING

    def test_the_recorded_copy_passes_a_deep_check(self, tmp_path):
        crash, item = _available_item(tmp_path)
        (deep,) = crash_service(crash).verify([item.id], deep=True)
        assert deep.kind is ResultKind.OK
        assert crash.state.library.get(item.id).status is ItemStatus.AVAILABLE


class _BlockingNetwork(StaticNetwork):
    """``current`` can be made to hang, holding the scheduler loop inside a pass."""

    def __init__(self) -> None:
        super().__init__()
        self.hang = threading.Event()
        self.inside = threading.Event()
        self.free = threading.Event()

    def current(self) -> NetworkKind:
        if self.hang.is_set():
            self.inside.set()
            self.free.wait(30)
        return super().current()


@pytest.mark.unit
class TestShutdown:
    def test_ownership_stays_until_the_loop_really_ended_and_close_can_wait_again(self, tmp_path):
        network = _BlockingNetwork()
        queue, _, _ = make_queue(tmp_path, FakeSources([], {}), network=network)
        queue.start()
        try:
            network.hang.set()
            queue.wake()
            assert network.inside.wait(10)
            assert queue.close(timeout=0.2) is False
            assert queue.running
            assert ExecutorOwnership.try_acquire(tmp_path / "history.db") is None
            assert queue.close(timeout=0.2) is False
        finally:
            network.free.set()
        assert queue.close(timeout=10) is True
        assert not queue.running
        again = ExecutorOwnership.try_acquire(tmp_path / "history.db")
        assert again is not None
        again.release()


@pytest.mark.unit
class TestStoreOutage:
    def test_an_outcome_the_store_refused_is_written_once_it_recovers_and_the_queue_moves_on(self, tmp_path, monkeypatch):
        sink = RecordingSink()
        gate = GatedTransfer()
        failing = _failing(monkeypatch, SqliteDownloadQueue, "complete")
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, events=sink)
        first, second = queue.enqueue(SERIES, [episode(1), episode(2)], DownloadCriteria()).added
        failing.set()
        queue.start()
        try:
            assert gate.started.acquire(timeout=10)
            gate.release.set()
            wait_for(lambda: sink.of(StoreUnavailable))
            assert state.queue.get(first.id).state is JobState.FINALIZING
            assert [a.job.id for a in queue._scheduler.active] == [first.id]
            failing.clear()
            wait_for(lambda: state.queue.get(first.id).state is JobState.COMPLETED)
            wait_for(lambda: state.queue.get(second.id).state is JobState.COMPLETED)
        finally:
            failing.clear()
            gate.release.set()
            assert queue.close()

    def test_unreadable_preferences_stop_the_transfer_after_the_grace_period(self, tmp_path, monkeypatch):
        gate = GatedTransfer()
        gate.io_done.set()
        failing = _failing(monkeypatch, SqlitePreferencesStore, "get")
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            assert gate.started.acquire(timeout=10)
            (attempt,) = queue._scheduler.active
            failing.set()
            started = time.monotonic()
            wait_for(lambda: gate.active == 0, timeout=10)
            assert time.monotonic() - started >= fast_config().unreadable_stop_sec
            assert attempt.stop_kind == "store_unreadable"
            failing.clear()
            wait_for(lambda: state.queue.get(job.id).state is not JobState.STOPPING)
        finally:
            failing.clear()
            gate.release.set()
            queue.close()


@pytest.mark.unit
class TestConditionBookkeeping:
    @staticmethod
    def _scheduler(tmp_path, network: StaticNetwork) -> tuple[Scheduler, ExecutorOwnership]:
        """A fresh process: its own state, ownership and scheduler over the same database."""
        owner = ExecutorOwnership.try_acquire(tmp_path / "history.db")
        assert owner is not None
        ctx = Context(
            state=open_local_state(tmp_path / "history.db"),
            store=FsFileStore(tmp_path / "library"),
            sources=FakeSources([], {}),
            network=network,
            grants=Grants(),
            events=RecordingSink(),
            config=fast_config(),
        )
        return Scheduler(ctx, owner), owner

    @pytest.mark.parametrize("restart", [False, True])
    def test_a_full_disk_outlives_a_network_drop_and_a_restart(self, tmp_path, restart):
        state = open_local_state(tmp_path / "history.db")
        (job,) = state.queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        network = StaticNetwork()
        scheduler, owner = self._scheduler(tmp_path, network)
        try:
            claim = state.queue.claim_next(NetworkKind.WIFI, owner)
            assert state.queue.wait(claim.attempt_id, WaitReason.STORAGE)
            network.set(NetworkKind.OFFLINE)
            scheduler.tick()
            if restart:
                owner.release()
                scheduler, owner = self._scheduler(tmp_path, network)
            network.set(NetworkKind.WIFI)
            # free space is unknown against an unknown remainder, so nothing proves the disk was freed
            scheduler.tick()
            current = state.queue.get(job.id)
            assert (current.state, current.wait_reason) == (JobState.WAITING, WaitReason.STORAGE)
            assert scheduler.active == []
        finally:
            owner.release()

    def test_a_full_disk_still_lifts_on_evidence_while_offline(self, tmp_path):
        state = open_local_state(tmp_path / "history.db")
        (job,) = state.queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        network = StaticNetwork()
        scheduler, owner = self._scheduler(tmp_path, network)
        try:
            claim = state.queue.claim_next(NetworkKind.WIFI, owner)
            assert state.queue.wait(claim.attempt_id, WaitReason.STORAGE)
            network.set(NetworkKind.OFFLINE)
            scheduler.notify_storage_changed()
            scheduler.tick()
            current = state.queue.get(job.id)
            assert (current.state, current.wait_reason) == (JobState.WAITING, WaitReason.NETWORK)
        finally:
            owner.release()
