from dataclasses import replace

import pytest

from alt_ani_core.errors import InvalidTransitionError, StateConflictError, UnknownRecordError
from alt_ani_core.state import ItemStatus, JobError, JobState, NetworkKind, OpenedVia, StorageLocator

from .state_support import download, enqueue, key, open_state, ref, row, spec


@pytest.fixture
def state(tmp_path):
    return open_state(tmp_path / "history.db")


@pytest.mark.unit
class TestOfflineItems:
    def test_completed_job_creates_an_available_item_with_what_was_saved(self, state):
        job, item = download(state, 1)
        assert job.state is JobState.COMPLETED and job.offline_item_id == item.id
        assert item.status is ItemStatus.AVAILABLE and item.key == key(1)
        assert item.spec == spec("fate - ep1.mp4")
        assert state.library.items() == [item]
        assert state.library.for_episode(key(1)) == [item]

    def test_platform_locator_is_kept_opaque(self, state):
        enqueue(state, 2)
        claim = state.queue.claim_next(NetworkKind.WIFI)
        state.queue.begin_finalize(claim.attempt_id)
        uri = "content://com.android.externalstorage.documents/document/primary%3AMovies%2Ffate.mp4"
        item = state.queue.complete(claim.attempt_id, spec(uri, backend="saf"))
        assert state.library.get(item.id).spec.locator == StorageLocator("saf", uri)

    def test_one_locator_is_one_item(self, state):
        download(state, 1, name="same.mp4")
        enqueue(state, 2)
        claim = state.queue.claim_next(NetworkKind.WIFI)
        state.queue.begin_finalize(claim.attempt_id)
        with pytest.raises(StateConflictError) as info:
            state.queue.complete(claim.attempt_id, spec("same.mp4"))
        assert info.value.code == "state.locator_in_use"
        assert state.queue.get(claim.job.id).state is JobState.FINALIZING
        # the executor can still end the attempt, so the slot is not held forever
        assert state.queue.fail(claim.attempt_id, JobError("download.target_taken"))
        assert state.queue.claim_next(NetworkKind.WIFI) is None
        assert state.queue.get(claim.job.id).state is JobState.FAILED

    def test_redownload_to_the_locator_of_a_missing_copy_reuses_its_row(self, state):
        _, item = download(state, 1)
        state.library.mark_missing(item.id)
        (job,) = enqueue(state, 1).added
        claim = state.queue.claim_next(NetworkKind.WIFI)
        state.queue.begin_finalize(claim.attempt_id)
        restored = state.queue.complete(claim.attempt_id, replace(spec("fate - ep1.mp4"), size_bytes=2000))
        assert (restored.id, restored.status, restored.spec.size_bytes) == (item.id, ItemStatus.AVAILABLE, 2000)
        assert state.queue.get(job.id).offline_item_id == item.id
        assert state.library.items() == [restored]

    def test_locator_of_a_copy_being_deleted_is_a_conflict(self, state):
        _, item = download(state, 1)
        state.library.mark_deleting([item.id])
        enqueue(state, 1)
        claim = state.queue.claim_next(NetworkKind.WIFI)
        state.queue.begin_finalize(claim.attempt_id)
        with pytest.raises(StateConflictError):
            state.queue.complete(claim.attempt_id, spec("fate - ep1.mp4"))
        assert state.library.get(item.id).status is ItemStatus.DELETING

    def test_missing_copy_of_another_episode_is_not_taken_over(self, state):
        _, item = download(state, 1, name="shared.mp4")
        state.library.mark_missing(item.id)
        enqueue(state, 2)
        claim = state.queue.claim_next(NetworkKind.WIFI)
        state.queue.begin_finalize(claim.attempt_id)
        with pytest.raises(StateConflictError):
            state.queue.complete(claim.attempt_id, spec("shared.mp4"))

    def test_missing_and_back(self, state):
        _, item = download(state)
        assert state.library.mark_missing(item.id).status is ItemStatus.MISSING
        restored = state.library.mark_available(item.id, 900)
        assert (restored.status, restored.spec.size_bytes) == (ItemStatus.AVAILABLE, 900)

    def test_filters(self, state):
        _, a = download(state, 1)
        _, b = download(state, 2)
        state.library.mark_missing(b.id)
        assert state.library.items(status=ItemStatus.MISSING) == [state.library.get(b.id)]
        assert state.library.items(series_id="other") == []

    def test_unknown_item(self, state):
        with pytest.raises(UnknownRecordError) as info:
            state.library.get("nope")
        assert info.value.code == "state.unknown_item"


@pytest.mark.unit
class TestDeletion:
    def test_forget_requires_marking_first(self, state):
        _, item = download(state)
        with pytest.raises(InvalidTransitionError):
            state.library.forget(item.id)

    def test_deleting_keeps_watch_state_and_job_history(self, state):
        state.watch.record_opened(ref(), row(1), OpenedVia.PLAYER)
        session = state.watch.begin_playback(key(1))
        state.watch.record_playback_ended(session, 1, 100, 100)
        job, item = download(state, 1)
        state.library.mark_deleting([item.id])
        state.library.forget(item.id)
        assert state.library.items() == []
        assert state.watch.get(key(1)).watched
        after = state.queue.get(job.id)
        assert after.state is JobState.COMPLETED and after.offline_item_id is None

    def test_cancelling_or_retrying_other_jobs_leaves_items_alone(self, state):
        _, item = download(state, 1)
        (other,) = enqueue(state, 2).added
        state.queue.cancel(other.id)
        assert state.library.get(item.id).status is ItemStatus.AVAILABLE

    def test_deleted_episode_can_be_downloaded_again(self, state):
        _, item = download(state, 1)
        state.library.mark_deleting([item.id])
        state.library.forget(item.id)
        assert len(enqueue(state, 1).added) == 1

    def test_deleting_item_is_not_available_for_skipping(self, state):
        _, item = download(state, 1)
        state.library.mark_deleting([item.id])
        assert len(enqueue(state, 1).added) == 1
