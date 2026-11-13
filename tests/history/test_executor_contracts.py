"""Store contracts added for the stage-2 executor: ownership, deferral, target reservation and pending removals."""

import subprocess
import sys
import textwrap
import time
from dataclasses import replace

import pytest

from alt_ani_core.errors import ExecutorNotOwnerError, StateConflictError
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.state import (
    ItemStatus,
    JobError,
    JobState,
    NetworkKind,
    RemovalReason,
    StorageLocator,
    WaitReason,
)

from .state_support import OTHER_SOURCE, SOURCE, VARIANT, download, enqueue, open_state, owner, partial, spec

WIFI = NetworkKind.WIFI


@pytest.fixture
def state(tmp_path):
    return open_state(tmp_path / "history.db")


def _running(state, number: float = 1):
    (job,) = enqueue(state, number).added
    claim = state.queue.claim_next(WIFI, owner(state))
    assert claim is not None and claim.job.id == job.id
    return claim


def _hold_in_child(db, ready_marker) -> subprocess.Popen:
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from alt_ani_core.history.ownership import ExecutorOwnership
        owner = ExecutorOwnership.try_acquire(Path(sys.argv[1]))
        Path(sys.argv[2]).write_text("held" if owner else "busy")
        time.sleep(120)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(db), str(ready_marker)])
    for _ in range(600):
        if ready_marker.exists() and ready_marker.read_text():
            break
        time.sleep(0.05)
    return child


@pytest.mark.unit
class TestExecutorOwnership:
    def test_second_owner_in_the_same_process_is_refused(self, tmp_path):
        db = tmp_path / "history.db"
        first = ExecutorOwnership.try_acquire(db)
        assert first is not None
        assert ExecutorOwnership.try_acquire(db) is None
        first.release()
        again = ExecutorOwnership.try_acquire(db)
        assert again is not None
        again.release()

    def test_another_process_cannot_take_ownership_until_the_owner_dies(self, tmp_path):
        db = tmp_path / "history.db"
        marker = tmp_path / "ready"
        child = _hold_in_child(db, marker)
        try:
            assert marker.read_text() == "held"
            assert ExecutorOwnership.try_acquire(db) is None
        finally:
            child.kill()
            child.wait(timeout=30)
        # Windows drops a dead process's locks asynchronously, and a venv launcher's interpreter dies after it
        deadline = time.monotonic() + 30
        while (taken := ExecutorOwnership.try_acquire(db)) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert taken is not None
        taken.release()

    def test_claim_and_recovery_need_the_owner_of_this_database(self, state, tmp_path):
        enqueue(state, 1)
        other = ExecutorOwnership.try_acquire(tmp_path / "other.db")
        try:
            with pytest.raises(ExecutorNotOwnerError):
                state.queue.claim_next(WIFI, other)
            with pytest.raises(ExecutorNotOwnerError):
                state.queue.recover_interrupted(other)
        finally:
            other.release()
        assert state.queue.jobs()[0].state is JobState.QUEUED

    def test_a_released_ownership_no_longer_authorises(self, state, tmp_path):
        enqueue(state, 1)
        held = ExecutorOwnership.try_acquire(tmp_path / "history.db")
        held.release()
        with pytest.raises(ExecutorNotOwnerError):
            state.queue.claim_next(WIFI, held)


@pytest.mark.unit
class TestDefer:
    def test_running_attempt_goes_back_to_the_queue_with_a_retry_spent(self, state):
        claim = _running(state)
        assert state.queue.defer(claim.attempt_id, JobError("transfer.timeout"), "2026-01-01T09:00:00+00:00")
        job = state.queue.get(claim.job.id)
        assert (job.state, job.retry_count, job.not_before, job.attempt_id) == (
            JobState.QUEUED,
            1,
            "2026-01-01T09:00:00+00:00",
            None,
        )
        assert job.last_error == JobError("transfer.timeout")

    def test_job_is_not_claimed_before_its_time(self, state):
        claim = _running(state)
        state.queue.defer(claim.attempt_id, JobError("transfer.timeout"), "2999-01-01T00:00:00+00:00")
        assert state.queue.claim_next(WIFI, owner(state)) is None

    def test_job_due_now_is_claimed_and_its_delay_cleared(self, state):
        claim = _running(state)
        state.queue.defer(claim.attempt_id, JobError("transfer.timeout"), "2000-01-01T00:00:00+00:00")
        again = state.queue.claim_next(WIFI, owner(state))
        assert again is not None and again.job.not_before is None and again.job.retry_count == 1

    def test_uncounted_deferral_spends_no_retry(self, state):
        claim = _running(state)
        state.queue.defer(claim.attempt_id, JobError("executor.shutdown"), None, counts=False)
        assert state.queue.get(claim.job.id).retry_count == 0

    @pytest.mark.parametrize(
        ("stop", "expected"),
        [
            (lambda q, j: q.pause(j), (JobState.PAUSED, None)),
            (lambda q, j: q.cancel(j), (JobState.CANCELLED, None)),
            (lambda q, j: q.hold(j, WaitReason.NETWORK), (JobState.WAITING, WaitReason.NETWORK)),
            (lambda q, j: q.hold(j, WaitReason.STORAGE), (JobState.WAITING, WaitReason.STORAGE)),
        ],
    )
    def test_a_pending_stop_is_settled_as_requested(self, state, stop, expected):
        claim = _running(state)
        stop(state.queue, claim.job.id)
        assert state.queue.defer(claim.attempt_id, JobError("executor.shutdown"), None)
        job = state.queue.get(claim.job.id)
        assert (job.state, job.wait_reason) == expected
        assert job.retry_count == 0 and job.attempt_id is None

    def test_released_hold_settles_back_in_the_queue(self, state):
        claim = _running(state)
        state.queue.hold(claim.job.id, WaitReason.NETWORK)
        state.queue.release(claim.job.id, WaitReason.NETWORK)
        state.queue.defer(claim.attempt_id, JobError("executor.shutdown"), None)
        assert state.queue.get(claim.job.id).state is JobState.QUEUED

    def test_lost_or_finalizing_attempt_cannot_defer(self, state):
        claim = _running(state)
        state.queue.begin_finalize(claim.attempt_id)
        assert not state.queue.defer(claim.attempt_id, JobError("x"), None)
        assert not state.queue.defer("nope", JobError("x"), None)

    @pytest.mark.parametrize("action", ["retry", "resume", "choose_source"])
    def test_user_decisions_reset_the_retry_budget(self, state, action):
        claim = _running(state)
        state.queue.defer(claim.attempt_id, JobError("t"), "2999-01-01T00:00:00+00:00")
        state.queue.pause(claim.job.id)
        if action == "retry":
            state.queue.cancel(claim.job.id)
            (job,) = enqueue(state, 2).added
            again = state.queue.claim_next(WIFI, owner(state))
            state.queue.fail(again.attempt_id, JobError("x"))
            state.queue.retry(job.id)
            assert (state.queue.get(job.id).retry_count, state.queue.get(job.id).not_before) == (0, None)
            return
        if action == "resume":
            state.queue.resume(claim.job.id)
        else:
            state.queue.choose_source(claim.job.id, SOURCE, VARIANT)
        job = state.queue.get(claim.job.id)
        assert (job.retry_count, job.not_before) == (0, None)


@pytest.mark.unit
class TestTargetReservation:
    def test_target_of_another_open_job_is_refused(self, state):
        first = _running(state, 1)
        state.queue.set_target(first.attempt_id, StorageLocator("fs", "a.mp4"))
        state.queue.pause(first.job.id)
        state.queue.ack_stopped(first.attempt_id)
        second = _running(state, 2)
        with pytest.raises(StateConflictError) as exc:
            state.queue.set_target(second.attempt_id, StorageLocator("fs", "a.mp4"))
        assert exc.value.code == "state.locator_in_use"
        assert state.queue.get(second.job.id).target is None

    @pytest.mark.parametrize("status", [ItemStatus.AVAILABLE, ItemStatus.DELETING])
    def test_locator_of_a_kept_or_deleting_item_is_refused_before_publishing(self, state, status):
        _, item = download(state, 1, "taken.mp4")
        if status is ItemStatus.DELETING:
            state.library.mark_deleting([item.id])
        claim = _running(state, 2)
        with pytest.raises(StateConflictError):
            state.queue.set_target(claim.attempt_id, StorageLocator("fs", "taken.mp4"))

    def test_missing_copy_of_the_same_episode_may_be_replaced(self, state):
        _, item = download(state, 1, "ep1.mp4")
        state.library.mark_missing(item.id)
        state.library.mark_deleting([item.id])
        state.library.forget(item.id)
        _, item = download(state, 1, "ep1.mp4")
        state.library.mark_missing(item.id)
        claim = _running(state, 1)
        assert state.queue.set_target(claim.attempt_id, StorageLocator("fs", "ep1.mp4")).value == "continue"

    def test_missing_copy_of_another_episode_is_refused(self, state):
        _, item = download(state, 1, "ep1.mp4")
        state.library.mark_missing(item.id)
        claim = _running(state, 2)
        with pytest.raises(StateConflictError):
            state.queue.set_target(claim.attempt_id, StorageLocator("fs", "ep1.mp4"))

    def test_deleting_a_reserved_locator_is_refused_until_the_job_ends(self, state):
        _, item = download(state, 1, "ep1.mp4")
        state.library.mark_missing(item.id)
        claim = _running(state, 1)
        state.queue.set_target(claim.attempt_id, StorageLocator("fs", "ep1.mp4"))
        with pytest.raises(StateConflictError) as exc:
            state.library.mark_deleting([item.id])
        assert exc.value.code == "state.locator_reserved"
        state.queue.cancel(claim.job.id)
        state.queue.ack_stopped(claim.attempt_id)
        assert state.library.mark_deleting([item.id])[0].status is ItemStatus.DELETING


@pytest.mark.unit
class TestPendingRemovals:
    def _with_partial(self, state, number: float = 1, key: str = ".work/j/p1/"):
        claim = _running(state, number)
        state.queue.set_partial(claim.attempt_id, replace(partial(), locator=StorageLocator("fs", key)), 10)
        return claim

    def test_cancelling_a_running_job_queues_its_partial_once_io_ended(self, state):
        claim = self._with_partial(state)
        state.queue.cancel(claim.job.id)
        assert state.queue.pending_removals() == []
        state.queue.ack_stopped(claim.attempt_id)
        (removal,) = state.queue.pending_removals()
        assert (removal.locator.key, removal.reason, removal.job_id) == (".work/j/p1/", RemovalReason.CANCELLED, claim.job.id)

    def test_cancelling_a_paused_job_queues_its_partial(self, state):
        claim = self._with_partial(state)
        state.queue.pause(claim.job.id)
        state.queue.ack_stopped(claim.attempt_id)
        state.queue.cancel(claim.job.id)
        assert [r.reason for r in state.queue.pending_removals()] == [RemovalReason.CANCELLED]

    def test_a_new_partial_queues_the_old_one(self, state):
        claim = self._with_partial(state)
        state.queue.set_partial(claim.attempt_id, replace(partial(), locator=StorageLocator("fs", ".work/j/p2/")))
        state.queue.set_partial(
            claim.attempt_id, replace(partial(source=OTHER_SOURCE), locator=StorageLocator("fs", ".work/j/p3/"))
        )
        assert [(r.locator.key, r.reason) for r in state.queue.pending_removals()] == [
            (".work/j/p1/", RemovalReason.RESTARTED),
            (".work/j/p2/", RemovalReason.SOURCE_CHANGED),
        ]

    def test_choosing_another_source_queues_the_partial(self, state):
        claim = self._with_partial(state)
        state.queue.wait(claim.attempt_id, WaitReason.SOURCE_DECISION)
        state.queue.choose_source(claim.job.id, OTHER_SOURCE, VARIANT)
        assert [r.reason for r in state.queue.pending_removals()] == [RemovalReason.SOURCE_CHANGED]

    def test_keeping_the_partial_queues_nothing(self, state):
        claim = self._with_partial(state)
        state.queue.wait(claim.attempt_id, WaitReason.SOURCE_DECISION)
        state.queue.choose_source(claim.job.id, SOURCE, VARIANT)
        assert state.queue.pending_removals() == []

    def test_completion_queues_the_work_directory_and_keeps_the_digest(self, state):
        claim = self._with_partial(state)
        state.queue.begin_finalize(claim.attempt_id)
        item = state.queue.complete(claim.attempt_id, spec("ep1.mp4"))
        assert item.spec.sha256 == spec().sha256
        assert [r.reason for r in state.queue.pending_removals()] == [RemovalReason.COMPLETED]

    def test_forgetting_is_idempotent(self, state):
        claim = self._with_partial(state)
        state.queue.cancel(claim.job.id)
        state.queue.ack_stopped(claim.attempt_id)
        (removal,) = state.queue.pending_removals()
        state.queue.forget_removal(removal.id)
        state.queue.forget_removal(removal.id)
        assert state.queue.pending_removals() == []

    def test_a_locator_pending_removal_cannot_be_reserved(self, state):
        claim = self._with_partial(state, key="ep9.mp4")
        state.queue.cancel(claim.job.id)
        state.queue.ack_stopped(claim.attempt_id)
        other = _running(state, 2)
        with pytest.raises(StateConflictError):
            state.queue.set_target(other.attempt_id, StorageLocator("fs", "ep9.mp4"))
