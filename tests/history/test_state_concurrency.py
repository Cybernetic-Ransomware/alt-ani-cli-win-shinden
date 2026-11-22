import sqlite3
import threading
from contextlib import closing

import pytest

from alt_ani_core.errors import StateConflictError
from alt_ani_core.state import AttemptStatus, JobState, NetworkKind, SkipReason

from .state_support import enqueue, key, open_state, owner


@pytest.fixture
def path(tmp_path):
    db = tmp_path / "history.db"
    open_state(db).preferences.get()
    return db


def _together(*calls):
    """Runs the calls on separate threads released by one barrier; returns results or exceptions in call order."""
    barrier = threading.Barrier(len(calls))
    results: list[object] = [None] * len(calls)

    def run(index, call):
        barrier.wait()
        try:
            results[index] = call()
        except BaseException as exc:
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    return results


@pytest.mark.unit
class TestConcurrentWrites:
    def test_same_episode_added_twice_at_once_gives_one_job(self, path):
        first, second = open_state(path), open_state(path)
        results = _together(lambda: enqueue(first, 1), lambda: enqueue(second, 1))
        assert sorted(len(r.added) for r in results) == [0, 1]
        assert [r.skipped for r in results if not r.added] == [((key(1), SkipReason.ALREADY_QUEUED),)]
        assert len(open_state(path).queue.jobs()) == 1

    def test_concurrent_claims_never_exceed_one_active_download(self, path):
        enqueue(open_state(path), 1, 2)
        a, b = open_state(path), open_state(path)
        held = owner(a)
        claims = _together(
            lambda: a.queue.claim_next(NetworkKind.WIFI, held), lambda: b.queue.claim_next(NetworkKind.WIFI, held)
        )
        assert sum(c is not None for c in claims) == 1

    def test_progress_racing_a_pause_never_loses_the_pause(self, path):
        state = open_state(path)
        enqueue(state, 1)
        claim = state.queue.claim_next(NetworkKind.WIFI, owner(state))
        user, executor = open_state(path), open_state(path)
        _, status = _together(
            lambda: user.queue.pause(claim.job.id), lambda: executor.queue.report_progress(claim.attempt_id, 7)
        )
        job = state.queue.get(claim.job.id)
        assert job.state is JobState.STOPPING and job.attempt_id == claim.attempt_id
        assert status in (AttemptStatus.CONTINUE, AttemptStatus.STOP)
        assert state.queue.report_progress(claim.attempt_id, 8) is AttemptStatus.STOP

    def test_reorder_racing_an_enqueue_loses_no_job(self, path):
        state = open_state(path)
        a, b = enqueue(state, 1, 2).added
        revision = state.queue.queue_revision()
        other = open_state(path)
        reordered, _ = _together(lambda: state.queue.reorder([b.id, a.id], revision), lambda: enqueue(other, 3))
        jobs = [j.key.number for j in state.queue.jobs()]
        if isinstance(reordered, StateConflictError):
            assert jobs == [1.0, 2.0, 3.0]
        else:
            assert jobs == [2.0, 1.0, 3.0]

    def test_a_held_write_lock_delays_but_does_not_fail_an_operation(self, path):
        state = open_state(path)
        started = threading.Event()
        result: list[object] = []

        def add():
            started.set()
            result.append(enqueue(state, 1))

        with closing(sqlite3.connect(path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            thread = threading.Thread(target=add)
            thread.start()
            started.wait()
            thread.join(timeout=0.5)
            assert thread.is_alive()
            blocker.execute("COMMIT")
        thread.join(timeout=60)
        assert len(result[0].added) == 1
