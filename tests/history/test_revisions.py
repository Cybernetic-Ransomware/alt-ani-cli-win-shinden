"""Stage-3 guards: every user mutation checks the revision it was decided on, in the transaction that writes."""

import pytest

from alt_ani_core.errors import InvalidTransitionError, StateConflictError
from alt_ani_core.rules.job_state import available_actions
from alt_ani_core.state import (
    DownloadCriteria,
    JobAction,
    JobError,
    JobState,
    NetworkKind,
    OpenedVia,
    SkipReason,
    WaitReason,
)

from .state_support import SOURCE, enqueue, key, open_state, owner, ref, row, rows


@pytest.fixture
def state(tmp_path):
    return open_state(tmp_path / "history.db")


def _claim(state):
    claim = state.queue.claim_next(NetworkKind.WIFI, owner(state))
    assert claim is not None
    return claim


def _interaction_wait(state):
    (job,) = enqueue(state, 1).added
    claim = _claim(state)
    assert state.queue.wait(claim.attempt_id, WaitReason.INTERACTION, JobError("interaction.required", (("scope", "host"),)))
    return state.queue.get(job.id)


@pytest.mark.unit
class TestDelayedRepeats:
    def test_pause_decided_before_a_resume_is_refused(self, state):
        (job,) = enqueue(state, 1).added
        paused = state.queue.pause(job.id, job.revision)
        resumed = state.queue.resume(paused.id, paused.revision)
        with pytest.raises(StateConflictError) as err:
            state.queue.pause(job.id, job.revision)
        assert err.value.code == "state.conflict"
        assert err.value.params["found"] == resumed.revision
        assert state.queue.get(job.id).state is JobState.QUEUED

    def test_retry_decided_on_an_earlier_failure_is_refused(self, state):
        (job,) = enqueue(state, 1).added
        assert state.queue.fail(_claim(state).attempt_id, JobError("x"))
        failed = state.queue.get(job.id)
        state.queue.retry(job.id, failed.revision)
        assert state.queue.fail(_claim(state).attempt_id, JobError("x"))
        with pytest.raises(StateConflictError):
            state.queue.retry(job.id, failed.revision)
        assert state.queue.get(job.id).state is JobState.FAILED

    def test_cellular_consent_after_a_revocation_is_refused(self, state):
        (job,) = enqueue(state, 1).added
        allowed = state.queue.allow_cellular(job.id, True, job.revision)
        state.queue.allow_cellular(job.id, False, allowed.revision)
        with pytest.raises(StateConflictError):
            state.queue.allow_cellular(job.id, True, job.revision)
        assert state.queue.get(job.id).cellular_allowed is False

    def test_choose_source_and_cancel_check_the_revision(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.choose_source(job.id, SOURCE, expected_revision=job.revision)
        with pytest.raises(StateConflictError):
            state.queue.choose_source(job.id, SOURCE, expected_revision=job.revision)
        with pytest.raises(StateConflictError):
            state.queue.cancel(job.id, job.revision)

    def test_no_revision_keeps_the_old_behaviour(self, state):
        (job,) = enqueue(state, 1).added
        assert state.queue.pause(job.id).state is JobState.PAUSED


@pytest.mark.unit
class TestPreviewBasis:
    def test_preview_reports_the_newest_sequence(self, state):
        assert state.queue.preview("1", [1]).basis_seq == 0
        enqueue(state, 1, 2)
        assert state.queue.preview("1", [3]).basis_seq == 2

    def test_delayed_repeat_after_cancel_does_not_add_again(self, state):
        basis = state.queue.preview("1", [1, 2]).basis_seq
        first = state.queue.enqueue(ref(), rows(1, 2), DownloadCriteria(), basis_seq=basis)
        state.queue.cancel(first.added[0].id)
        again = state.queue.enqueue(ref(), rows(1, 2), first.added[0].criteria, basis_seq=basis)
        assert again.added == ()
        assert {(k.number, r) for k, r in again.skipped} == {
            (1.0, SkipReason.CHANGED_SINCE_PREVIEW),
            (2.0, SkipReason.CHANGED_SINCE_PREVIEW),
        }

    def test_without_basis_a_cancelled_episode_can_be_added_again(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.cancel(job.id)
        assert len(enqueue(state, 1).added) == 1


@pytest.mark.unit
class TestRetryInteraction:
    def test_releases_with_a_fresh_budget(self, state):
        job = _interaction_wait(state)
        assert JobAction.RETRY_INTERACTION in available_actions(job)
        retried = state.queue.retry_interaction(job.id, job.revision)
        assert (retried.state, retried.wait_reason, retried.retry_count) == (JobState.QUEUED, None, 0)

    def test_stale_answer_is_a_conflict(self, state):
        job = _interaction_wait(state)
        state.queue.retry_interaction(job.id, job.revision)
        with pytest.raises(StateConflictError):
            state.queue.retry_interaction(job.id, job.revision)

    def test_only_from_an_interaction_wait(self, state):
        (job,) = enqueue(state, 1).added
        with pytest.raises(InvalidTransitionError):
            state.queue.retry_interaction(job.id, job.revision)


@pytest.mark.unit
class TestAvailableActionsMatchTheStore:
    def _jobs(self, state):
        (queued,) = enqueue(state, 1).added
        (paused,) = enqueue(state, 2).added
        paused = state.queue.pause(paused.id)
        (failed,) = enqueue(state, 3).added
        state.queue.pause(queued.id)
        claim = _claim(state)
        state.queue.fail(claim.attempt_id, JobError("x"))
        state.queue.resume(queued.id)
        (cancelled,) = enqueue(state, 4).added
        state.queue.cancel(cancelled.id)
        return [state.queue.get(j.id) for j in (queued, paused, failed, cancelled)]

    def test_offered_actions_succeed_and_others_are_refused(self, tmp_path):
        calls = {
            JobAction.PAUSE: lambda q, j: q.pause(j.id, j.revision),
            JobAction.RESUME: lambda q, j: q.resume(j.id, j.revision),
            JobAction.CANCEL: lambda q, j: q.cancel(j.id, j.revision),
            JobAction.RETRY: lambda q, j: q.retry(j.id, j.revision),
            JobAction.ALLOW_CELLULAR: lambda q, j: q.allow_cellular(j.id, True, j.revision),
            JobAction.CHOOSE_SOURCE: lambda q, j: q.choose_source(j.id, SOURCE, expected_revision=j.revision),
            JobAction.RETRY_INTERACTION: lambda q, j: q.retry_interaction(j.id, j.revision),
        }
        for action, call in calls.items():
            state = open_state(tmp_path / f"{action.value}.db")
            for job in self._jobs(state):
                offered = action in available_actions(job)
                try:
                    changed = call(state.queue, job).revision != job.revision
                except InvalidTransitionError:
                    changed = False
                assert changed == offered, (action, job.state)


@pytest.mark.unit
class TestWatchRevisions:
    def test_start_playback_opens_and_begins_in_one_step(self, state):
        session, watch = state.watch.start_playback(ref(), row(1))
        assert watch.opened_via is OpenedVia.PLAYER
        assert watch.playback_session_id == session.session_id
        newer, _ = state.watch.start_playback(ref(), row(1))
        assert state.watch.record_position(session, 1, 10) is False
        assert state.watch.record_position(newer, 1, 10) is True

    def test_manual_change_needs_the_current_revision(self, state):
        state.watch.record_opened(ref(), row(1), OpenedVia.BROWSER)
        before = state.watch.get(key(1))
        marked = state.watch.set_watched(key(1), True, before.revision)
        state.watch.set_watched(key(1), False, marked.revision)
        with pytest.raises(StateConflictError):
            state.watch.set_watched(key(1), True, before.revision)
        assert state.watch.get(key(1)).watched is False

    def test_missing_row_is_revision_zero(self, state):
        state.queue.enqueue(ref(), rows(1), DownloadCriteria())
        assert state.watch.set_watched(key(1), True, 0).watched is True


@pytest.mark.unit
class TestRevisionReads:
    def test_progress_bumps_the_job_signal(self, state):
        (job,) = enqueue(state, 1).added
        claim = _claim(state)
        _, before = state.queue.revisions()
        state.queue.report_progress(claim.attempt_id, 10, 100)
        revision, after = state.queue.revisions()
        assert after[job.id].revision > before[job.id].revision
        assert (after[job.id].bytes_done, after[job.id].bytes_total) == (10, 100)
        assert revision == state.queue.queue_revision()

    def test_library_revisions(self, state):
        from .state_support import download

        _, item = download(state, 1)
        assert state.library.revisions() == {item.id: (item.status, item.revision)}
