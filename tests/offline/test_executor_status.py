import pytest

from alt_ani_core.errors import InvalidTransitionError
from alt_ani_core.services.offline import ExecutorState
from alt_ani_core.state import DownloadCriteria, JobError, JobState, WaitReason

from .support import SERIES, FakeSources, GatedTransfer, episode, make_queue, player, progressive, wait_for


@pytest.fixture
def gated(tmp_path):
    transfer = GatedTransfer()
    sources = FakeSources([player("1")], {"1": lambda fresh: progressive("http://127.0.0.1:9/v.mp4")})
    queue, state, network = make_queue(tmp_path, sources, transfer=transfer)
    yield queue, state, transfer
    transfer.io_done.set()
    transfer.release.set()
    queue.close(5)


@pytest.mark.unit
class TestExecutorStatus:
    def test_close_timeout_keeps_closing_until_the_io_ends(self, gated):
        queue, state, transfer = gated
        assert queue.status().state is ExecutorState.STOPPED
        queue.start()
        assert queue.status().state is ExecutorState.RUNNING
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        assert transfer.started.acquire(timeout=5)
        assert queue.close(timeout=0.05) is False
        status = queue.status()
        assert status.state is ExecutorState.CLOSING and status.stuck_jobs == (job.id,)
        with pytest.raises(InvalidTransitionError) as err:
            queue.start()
        assert err.value.code == "executor.closing"
        transfer.io_done.set()
        assert queue.close(timeout=5) is True
        assert queue.status().state is ExecutorState.STOPPED

    def test_retry_interaction_drops_any_grant_and_requeues(self, gated):
        queue, state, transfer = gated
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        assert transfer.started.acquire(timeout=5)
        queue.pause(job.id)
        transfer.io_done.set()
        wait_for(lambda: state.queue.get(job.id).state is JobState.PAUSED)
        assert queue.close(5)
        with state.database.write() as conn:
            conn.execute("UPDATE download_job SET state = 'waiting', wait_reason = 'interaction' WHERE id = ?", (job.id,))
        current = state.queue.get(job.id)
        assert queue.retry_interaction(job.id, current.revision).state is JobState.QUEUED
