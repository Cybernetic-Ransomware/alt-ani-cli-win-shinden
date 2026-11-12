"""Randomised user, scheduler and executor actions against the store: no claim while an earlier attempt's I/O runs."""

import random

import pytest

from alt_ani_core.history.local import open_local_state
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.state import AttemptStatus, DownloadCriteria, JobError, JobState, NetworkKind, WaitReason

from .support import SERIES, episode

SEEDS = range(200)
STEPS = 40


@pytest.mark.unit
@pytest.mark.parametrize("seed", SEEDS)
def test_no_second_attempt_before_the_first_ends_its_io(tmp_path, seed):
    rnd = random.Random(seed)
    state = open_local_state(tmp_path / "history.db")
    queue = state.queue
    jobs = [j.id for j in queue.enqueue(SERIES, [episode(n) for n in (1, 2, 3)], DownloadCriteria()).added]
    owner = ExecutorOwnership.try_acquire(tmp_path / "history.db")
    io: dict[str, str] = {}
    try:
        for _ in range(STEPS):
            action = rnd.choice(["claim", "pause", "resume", "cancel", "hold", "release", "progress", "end"])
            job_id = rnd.choice(jobs)
            try:
                if action == "claim":
                    claim = queue.claim_next(NetworkKind.WIFI, owner)
                    if claim is not None:
                        assert claim.job.id not in io, "claimed while an earlier attempt still does I/O"
                        io[claim.job.id] = claim.attempt_id
                        assert len(io) <= 1
                elif action == "pause":
                    queue.pause(job_id)
                elif action == "resume":
                    queue.resume(job_id)
                elif action == "cancel":
                    queue.cancel(job_id)
                elif action == "hold":
                    queue.hold(job_id, WaitReason.NETWORK)
                elif action == "release":
                    queue.release(job_id, WaitReason.NETWORK)
                elif action == "progress" and job_id in io:
                    if queue.report_progress(io[job_id], 10) is AttemptStatus.LOST:
                        io.pop(job_id)
                elif action == "end" and job_id in io:
                    attempt = io.pop(job_id)
                    ending = rnd.choice(["ack", "defer", "wait", "fail"])
                    if ending == "ack" and not queue.ack_stopped(attempt):
                        queue.defer(attempt, JobError("executor.stopped"), None, counts=False)
                    elif ending == "defer":
                        queue.defer(attempt, JobError("transfer.timeout"), None)
                    elif ending == "wait":
                        queue.wait(attempt, WaitReason.STORAGE)
                    elif ending == "fail":
                        queue.fail(attempt, JobError("x"))
            except Exception as exc:
                assert getattr(exc, "code", "").startswith("state."), exc
            active = queue.jobs([JobState.RUNNING, JobState.STOPPING, JobState.FINALIZING])
            assert {j.id for j in active} == set(io), "store and executor disagree about running attempts"
    finally:
        owner.release()
