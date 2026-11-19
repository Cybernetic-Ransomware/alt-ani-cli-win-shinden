"""Store outages, shutdown, concurrent deletes and the condition bookkeeping between scheduler passes."""

import hashlib
import itertools
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.history import database
from alt_ani_core.history.local import open_local_state
from alt_ani_core.history.ownership import DELETE_LOCK_SUFFIX, ExecutorOwnership, lock_path
from alt_ani_core.history.preferences import SqlitePreferencesStore
from alt_ani_core.history.queue import SqliteDownloadQueue
from alt_ani_core.rules.resume import KIND_COMPLETE, KIND_HTTP, checkpoint_of
from alt_ani_core.services.offline.attempt import Context
from alt_ani_core.services.offline.events import StoreUnavailable
from alt_ani_core.services.offline.interaction import Grants
from alt_ani_core.services.offline.library import LibraryService, ResultKind
from alt_ani_core.services.offline.network import StaticNetwork
from alt_ani_core.services.offline.scheduler import Scheduler
from alt_ani_core.state import DownloadCriteria, ItemStatus, JobState, NetworkKind, PartialTransfer, WaitReason
from alt_ani_core.storage.fs import FsFileStore, sha256_path
from alt_ani_core.testing import RecordingSink

from .servers import Resource, ServerState, random_body, serve
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


def _store_error(kind: str) -> sqlite3.Error:
    """Raised the way SQLite raises it: ``sqlite_errorcode`` is what tells a held lock from a broken file."""
    error: sqlite3.Error
    if kind == "busy":
        error, error.sqlite_errorcode = sqlite3.OperationalError("database is locked"), sqlite3.SQLITE_BUSY
    elif kind == "ioerr":
        error, error.sqlite_errorcode = sqlite3.OperationalError("disk I/O error"), sqlite3.SQLITE_IOERR
    elif kind == "uncoded":
        error = sqlite3.OperationalError("database is locked")
    else:
        error, error.sqlite_errorcode = sqlite3.IntegrityError("constraint failed"), sqlite3.SQLITE_CONSTRAINT
    return error


class _Fault:
    """Matching calls of one store method raise: the first ``times`` of them, and every one while ``held`` is set."""

    def __init__(self, monkeypatch, cls, name: str, kind: str = "busy", *, times: int = 0, when=None) -> None:
        self.held = threading.Event()
        self.raised = 0
        self._remaining = times
        self._lock = threading.Lock()
        real = getattr(cls, name)

        def wrapper(obj, *args, **kwargs):
            if when is None or when(*args, **kwargs):
                with self._lock:
                    fire = self.held.is_set() or self._remaining > 0
                    if fire:
                        self._remaining = max(self._remaining - 1, 0)
                        self.raised += 1
                if fire:
                    raise _store_error(kind)
            return real(obj, *args, **kwargs)

        monkeypatch.setattr(cls, name, wrapper)


def _validator_kind(_attempt_id: str, partial: PartialTransfer, *_rest) -> str:
    if partial.validator is None:
        return "none"
    if partial.validator.kind == KIND_COMPLETE:
        return "evidence"
    return "validator" if checkpoint_of(partial.validator) == (0, 0) else "checkpoint"


def _set_partial_with(kind: str):
    return lambda *args, **kwargs: _validator_kind(*args, **kwargs) == kind


def _published(tmp_path) -> list:
    return [p for p in (tmp_path / "library").rglob("*") if p.is_file() and ".work" not in p.relative_to(tmp_path).parts]


def _evidence_kind(job) -> str | None:
    return job.partial.validator.kind if job.partial and job.partial.validator else None


@pytest.fixture
def thread_errors(monkeypatch) -> list[BaseException]:
    """Exceptions that escaped any thread's target; an attempt must end every path inside ``run``."""
    errors: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    return errors


def _past_wall():
    """Far behind the database's clock but an hour later at each reading, so every deferral is due at the next pass."""
    readings = itertools.count()
    start = datetime.now(UTC) - timedelta(days=36500)
    return lambda: start + timedelta(hours=next(readings))


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


@pytest.mark.unit
class TestStoreFailureDuringAttempt:
    @pytest.mark.parametrize(
        ("name", "when"),
        [
            ("episode_of", None),
            ("set_partial", _set_partial_with("none")),
            ("set_partial", _set_partial_with("validator")),
            ("set_partial", _set_partial_with("evidence")),
            ("set_target", None),
            ("begin_finalize", None),
        ],
        ids=["episode_of", "partial", "validator", "evidence", "set_target", "begin_finalize"],
    )
    def test_one_held_lock_is_retried_in_place_and_the_job_completes_unchanged(
        self, tmp_path, monkeypatch, thread_errors, name, when
    ):
        fault = _Fault(monkeypatch, SqliteDownloadQueue, name, times=1, when=when)
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).state in (JobState.COMPLETED, JobState.FAILED))
        finally:
            assert queue.close()
        final = state.queue.get(job.id)
        assert fault.raised == 1
        assert (final.state, final.retry_count, final.last_error) == (JobState.COMPLETED, 0, None)
        assert gate.calls == 1
        assert len(_published(tmp_path)) == 1
        assert state.library.get(final.offline_item_id).status is ItemStatus.AVAILABLE
        assert thread_errors == []

    def test_a_lock_sqlite_really_reports_is_retried_in_place(self, tmp_path, monkeypatch):
        busy_timeout = database.BUSY_TIMEOUT_MS
        monkeypatch.setattr(database, "BUSY_TIMEOUT_MS", 20)
        holder = sqlite3.connect(tmp_path / "history.db", isolation_level=None, check_same_thread=False)
        seen: list[sqlite3.Error] = []
        real = SqliteDownloadQueue.set_target

        def locked_once(self, *args):
            if seen:
                return real(self, *args)
            holder.execute("BEGIN IMMEDIATE")
            try:
                return real(self, *args)
            except sqlite3.OperationalError as exc:
                seen.append(exc)
                raise
            finally:
                holder.execute("ROLLBACK")

        monkeypatch.setattr(SqliteDownloadQueue, "set_target", locked_once)
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added

        def ended() -> bool:
            # with the shortened busy timeout this poll may meet a commit of the executor itself
            try:
                return state.queue.get(job.id).state in (JobState.COMPLETED, JobState.FAILED)
            except sqlite3.OperationalError:
                return False

        queue.start()
        try:
            wait_for(ended)
        finally:
            assert queue.close()
            holder.close()
        monkeypatch.setattr(database, "BUSY_TIMEOUT_MS", busy_timeout)
        final = state.queue.get(job.id)
        assert (final.state, final.retry_count) == (JobState.COMPLETED, 0)
        assert seen and database.is_contention(seen[0])

    def test_a_lock_past_every_retry_requeues_the_job_later_without_spending_a_retry(self, tmp_path, monkeypatch):
        sink = RecordingSink()
        fault = _Fault(monkeypatch, SqliteDownloadQueue, "set_target")
        fault.held.set()
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, events=sink)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        before = datetime.now(UTC)
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).state is JobState.QUEUED and state.queue.get(job.id).last_error)
            wait_for(lambda: queue._scheduler.active == [])
            deferred = state.queue.get(job.id)
        finally:
            assert queue.close()
        assert (deferred.last_error.code, deferred.retry_count) == ("store.busy", 0)
        delay = (datetime.fromisoformat(deferred.not_before) - before).total_seconds()
        assert 29 <= delay <= 60
        assert deferred.partial is not None and _evidence_kind(deferred) == KIND_COMPLETE
        assert (tmp_path / "library" / deferred.partial.locator.key / "media.mp4").is_file()
        assert deferred.target is None and _published(tmp_path) == []
        assert [e.operation for e in sink.of(StoreUnavailable)] == ["set_target"]
        assert gate.calls == 1

    def test_a_lasting_lock_never_spends_the_retry_budget_and_the_job_completes_once_it_lifts(self, tmp_path, monkeypatch):
        sink = RecordingSink()
        fault = _Fault(monkeypatch, SqliteDownloadQueue, "begin_finalize")
        fault.held.set()
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, events=sink, wall=_past_wall())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: len(sink.of(StoreUnavailable)) >= 3)
            assert state.queue.get(job.id).retry_count == 0
            fault.held.clear()
            wait_for(lambda: state.queue.get(job.id).state is JobState.COMPLETED)
        finally:
            fault.held.clear()
            assert queue.close()
        final = state.queue.get(job.id)
        assert final.retry_count == 0
        # the evidence was recorded before the lock: every later attempt published from it instead of downloading again
        assert gate.calls == 1
        assert len(_published(tmp_path)) == 1

    @pytest.mark.parametrize("kind", ["ioerr", "uncoded"])
    def test_a_store_that_keeps_failing_otherwise_spends_retries_and_then_fails(self, tmp_path, monkeypatch, kind):
        fault = _Fault(monkeypatch, SqliteDownloadQueue, "set_target", kind)
        fault.held.set()
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, wall=_past_wall())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).state is JobState.FAILED)
        finally:
            fault.held.clear()
            assert queue.close()
        final = state.queue.get(job.id)
        assert (final.last_error.code, final.retry_count) == ("store.unavailable", 3)
        assert final.partial is not None and _published(tmp_path) == []

    @pytest.mark.parametrize(
        ("name", "when"),
        [("set_target", None), ("set_partial", _set_partial_with("validator"))],
        ids=["set_target", "validator"],
    )
    def test_a_defect_is_not_retried_and_fails_the_job(self, tmp_path, monkeypatch, thread_errors, name, when):
        fault = _Fault(monkeypatch, SqliteDownloadQueue, name, "defect", times=1, when=when)
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).state is JobState.FAILED)
        finally:
            assert queue.close()
        assert fault.raised == 1
        assert state.queue.get(job.id).last_error.code == "executor.unexpected"
        assert thread_errors == []

    def test_a_validator_the_store_refuses_ends_the_attempt_without_a_raw_store_error(
        self, tmp_path, monkeypatch, thread_errors
    ):
        fault = _Fault(monkeypatch, SqliteDownloadQueue, "set_partial", times=2, when=_set_partial_with("validator"))
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, wall=_past_wall())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).state in (JobState.COMPLETED, JobState.FAILED))
        finally:
            assert queue.close()
        final = state.queue.get(job.id)
        assert fault.raised == fast_config().store_retries
        assert (final.state, final.retry_count, final.last_error.code) == (JobState.COMPLETED, 0, "store.busy")
        # the first attempt ended inside the validator callback; nothing durable was recorded, so the second started afresh
        assert gate.calls == 2 and gate.resumed == [False]
        assert len(_published(tmp_path)) == 1
        assert thread_errors == []

    def test_a_pause_while_the_store_is_locked_settles_as_paused(self, tmp_path, monkeypatch):
        fault = _Fault(monkeypatch, SqliteDownloadQueue, "set_target")
        fault.held.set()
        gate = GatedTransfer()
        gate.release.set()
        # retries outlasting every wait below: only a retry wait the stop cuts short ends them in time
        config = fast_config(store_retries=1000, store_retry_delay_sec=0.05)
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, config=config)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: fault.raised >= 1)
            state.queue.pause(job.id)
            wait_for(lambda: state.queue.get(job.id).state is JobState.PAUSED)
            wait_for(lambda: queue._scheduler.active == [])
        finally:
            fault.held.clear()
            assert queue.close()
        final = state.queue.get(job.id)
        assert final.retry_count == 0 and final.partial is not None
        assert final.partial.locator not in {r.locator for r in state.queue.pending_removals()}

    def test_a_shutdown_while_the_store_is_locked_returns_the_job_unspent_and_a_restart_publishes_it(
        self, tmp_path, monkeypatch, thread_errors
    ):
        fault = _Fault(monkeypatch, SqliteDownloadQueue, "begin_finalize")
        fault.held.set()
        gate = GatedTransfer()
        gate.release.set()
        # retries outlasting every wait below: only a retry wait the stop cuts short ends them in time
        config = fast_config(store_retries=1000, store_retry_delay_sec=0.05)
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, config=config)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        wait_for(lambda: fault.raised >= 1)
        (attempt,) = queue._scheduler.active
        started = time.monotonic()
        assert queue.close()
        assert time.monotonic() - started < 5
        assert attempt.io_ended and attempt.done
        stopped = state.queue.get(job.id)
        assert (stopped.state, stopped.last_error.code, stopped.retry_count) == (JobState.QUEUED, "executor.shutdown", 0)
        fault.held.clear()
        again = GatedTransfer()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=again)
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).state is JobState.COMPLETED)
        finally:
            assert queue.close()
        assert again.calls == 0 and len(_published(tmp_path)) == 1
        assert thread_errors == []

    def test_an_outcome_the_store_refuses_through_close_is_recovered_at_the_next_start(self, tmp_path, monkeypatch):
        sink = RecordingSink()
        faults = [_Fault(monkeypatch, SqliteDownloadQueue, n) for n in ("begin_finalize", "defer", "ack_stopped")]
        for fault in faults:
            fault.held.set()
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, events=sink)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        wait_for(lambda: "defer" in [e.operation for e in sink.of(StoreUnavailable)])
        (attempt,) = queue._scheduler.active
        assert attempt.io_ended and not attempt.done
        assert queue.close()
        assert state.queue.get(job.id).state is JobState.RUNNING
        for fault in faults:
            fault.held.clear()
        again = GatedTransfer()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=again)
        report = queue.start()
        try:
            assert report.requeued == (job.id,)
            wait_for(lambda: state.queue.get(job.id).state is JobState.COMPLETED)
        finally:
            assert queue.close()
        assert state.queue.get(job.id).retry_count == 0
        assert again.calls == 0 and len(_published(tmp_path)) == 1

    def test_evidence_the_store_refused_is_recorded_with_the_outcome_and_the_file_is_published_without_a_download(
        self, tmp_path, monkeypatch, thread_errors
    ):
        sink = RecordingSink()
        evidence = _Fault(monkeypatch, SqliteDownloadQueue, "set_partial", when=_set_partial_with("evidence"))
        evidence.held.set()
        gate = GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, events=sink, wall=_past_wall())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: "defer" in [e.operation for e in sink.of(StoreUnavailable)])
            (attempt,) = queue._scheduler.active
            assert attempt.io_ended and not attempt.done
            assert state.queue.get(job.id).state is JobState.RUNNING
            evidence.held.clear()
            wait_for(lambda: state.queue.get(job.id).state is JobState.COMPLETED)
        finally:
            evidence.held.clear()
            assert queue.close()
        final = state.queue.get(job.id)
        assert final.retry_count == 0
        assert gate.calls == 1 and len(_published(tmp_path)) == 1
        assert thread_errors == []

    @pytest.mark.parametrize("restart", [False, True])
    def test_evidence_recorded_before_an_outcome_the_store_refuses_still_spares_the_download(
        self, tmp_path, monkeypatch, restart
    ):
        sink = RecordingSink()
        # the attempt's own evidence write runs out of retries; the first try of the outcome then records it
        retries = fast_config().store_retries
        _Fault(monkeypatch, SqliteDownloadQueue, "set_partial", times=retries, when=_set_partial_with("evidence"))
        defer = _Fault(monkeypatch, SqliteDownloadQueue, "defer")
        defer.held.set()
        gate, again = GatedTransfer(), GatedTransfer()
        gate.release.set()
        queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=gate, events=sink, wall=_past_wall())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: "defer" in [e.operation for e in sink.of(StoreUnavailable)])
            (attempt,) = queue._scheduler.active
            recorded = state.queue.get(job.id)
            assert (recorded.state, _evidence_kind(recorded)) == (JobState.RUNNING, KIND_COMPLETE)
            assert attempt.io_ended and not attempt.done
            if restart:
                assert queue.close()
                defer.held.clear()
                queue, state, _ = make_queue(tmp_path, _gated_sources(), transfer=again, wall=_past_wall())
                assert queue.start().requeued == (job.id,)
            defer.held.clear()
            wait_for(lambda: state.queue.get(job.id).state is JobState.COMPLETED)
        finally:
            defer.held.clear()
            assert queue.close()
        assert state.queue.get(job.id).retry_count == 0
        assert (gate.calls, again.calls) == (1, 0)
        assert len(_published(tmp_path)) == 1


@pytest.mark.loopback
class TestEvidenceLostThroughARestart:
    def test_the_complete_file_survives_and_the_job_waits_for_a_decision(self, tmp_path, monkeypatch):
        """The documented limit: without recorded evidence a restart cannot finalise, but nothing is removed."""
        body = random_body(200_000)
        server = ServerState()
        server.resources["/v.mp4"] = Resource(body, '"body-v1"')
        sink = RecordingSink()
        with serve(server) as url:
            sources = FakeSources([player("1")], {"1": lambda _f: progressive(f"{url}/v.mp4")})
            faults = [
                _Fault(monkeypatch, SqliteDownloadQueue, "set_partial", when=_set_partial_with("evidence")),
                _Fault(monkeypatch, SqliteDownloadQueue, "defer"),
                _Fault(monkeypatch, SqliteDownloadQueue, "ack_stopped"),
            ]
            for fault in faults:
                fault.held.set()
            queue, state, _ = make_queue(tmp_path, sources, events=sink)
            (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
            queue.start()
            wait_for(lambda: "defer" in [e.operation for e in sink.of(StoreUnavailable)])
            assert queue.close()
            for fault in faults:
                fault.held.clear()
            interrupted = state.queue.get(job.id)
            assert interrupted.state is JobState.RUNNING and _evidence_kind(interrupted) == KIND_HTTP
            queue, state, _ = make_queue(tmp_path, sources)
            queue.start()
            try:
                waiting = wait_for(
                    lambda: (j := state.queue.get(job.id)).state is JobState.WAITING and j,
                )
            finally:
                assert queue.close()
        assert (waiting.wait_reason, waiting.last_error.code) == (WaitReason.SOURCE_DECISION, "resume.impossible")
        assert waiting.partial.locator == interrupted.partial.locator
        assert waiting.partial.locator not in {r.locator for r in state.queue.pending_removals()}
        media = tmp_path / "library" / waiting.partial.locator.key / "media.mp4"
        assert sha256_path(media) == hashlib.sha256(body).hexdigest()
        assert _published(tmp_path) == []
