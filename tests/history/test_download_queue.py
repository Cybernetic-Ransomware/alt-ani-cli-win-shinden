import sqlite3
from contextlib import closing

import pytest

from alt_ani_core.errors import InvalidTransitionError, StateConflictError, UnknownRecordError
from alt_ani_core.history.queue import INTERRUPTED
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.state import (
    AlternativesPolicy,
    AttemptStatus,
    DownloadCriteria,
    DownloadNetwork,
    JobError,
    JobState,
    NetworkKind,
    QualityCriterion,
    SelectionMode,
    SkipReason,
    StopTarget,
    StorageLocator,
    WaitReason,
)

from .state_support import (
    OTHER_SOURCE,
    SOURCE,
    VARIANT,
    download,
    dump_database,
    enqueue,
    key,
    open_state,
    partial,
    ref,
    rows,
    spec,
)

WIFI, CELLULAR = NetworkKind.WIFI, NetworkKind.CELLULAR


@pytest.fixture
def state(tmp_path):
    return open_state(tmp_path / "history.db")


def _running(state, number: float = 1):
    (job,) = enqueue(state, number).added
    claim = state.queue.claim_next(WIFI)
    assert claim is not None and claim.job.id == job.id
    return claim


@pytest.mark.unit
class TestEnqueue:
    def test_jobs_keep_every_field(self, state):
        criteria = DownloadCriteria(audio="jp", subs="pl", quality=QualityCriterion(720))
        (job,) = enqueue(
            state,
            1,
            criteria=criteria,
            alternatives=AlternativesPolicy.COMPATIBLE,
            selection_mode=SelectionMode.MANUAL,
            source=SOURCE,
            variant=VARIANT,
            cellular_allowed=True,
        ).added
        got = state.queue.get(job.id)
        assert got == job
        assert (got.key, got.state, got.criteria, got.alternatives) == (
            key(1),
            JobState.QUEUED,
            criteria,
            AlternativesPolicy.COMPATIBLE,
        )
        assert (got.selection_mode, got.source, got.variant, got.cellular_allowed) == (
            SelectionMode.MANUAL,
            SOURCE,
            VARIANT,
            True,
        )
        assert (got.attempt_id, got.partial, got.target, got.last_error, got.bytes_done) == (None,) * 5

    def test_criteria_do_not_follow_later_preference_changes(self, state):
        from alt_ani_core.rules.criteria import criteria_for

        (job,) = enqueue(state, 1, criteria=criteria_for(state.preferences.get())).added
        state.preferences.update(audio="pl", subs="none", quality=QualityCriterion(480))
        assert state.queue.get(job.id).criteria == DownloadCriteria()

    def test_order_follows_the_request(self, state):
        enqueue(state, 3, 1, 2)
        assert [j.key.number for j in state.queue.jobs()] == [3.0, 1.0, 2.0]

    def test_episode_already_in_the_queue_is_skipped(self, state):
        enqueue(state, 1)
        result = enqueue(state, 1, 2)
        assert [j.key for j in result.added] == [key(2)]
        assert result.skipped == ((key(1), SkipReason.ALREADY_QUEUED),)

    def test_failed_job_still_blocks_a_duplicate(self, state):
        claim = _running(state)
        state.queue.fail(claim.attempt_id, JobError("download.failed"))
        assert enqueue(state, 1).skipped == ((key(1), SkipReason.ALREADY_QUEUED),)

    def test_cancelled_or_completed_job_does_not_block(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.cancel(job.id)
        assert len(enqueue(state, 1).added) == 1

    def test_available_copy_is_skipped(self, state):
        download(state, 1)
        assert enqueue(state, 1).skipped == ((key(1), SkipReason.ALREADY_DOWNLOADED),)

    def test_missing_copy_is_not_a_reason_to_skip(self, state):
        _, item = download(state, 1)
        state.library.mark_missing(item.id)
        assert len(enqueue(state, 1).added) == 1

    def test_legacy_download_record_is_not_proof_of_a_file(self, state):
        SqliteHistory(state.database).record_download(ref(), 1)
        assert len(enqueue(state, 1).added) == 1

    def test_duplicates_within_one_request_are_added_once(self, state):
        assert len(enqueue(state, 1, 1).added) == 1

    def test_preview_matches_enqueue_and_writes_nothing(self, state):
        enqueue(state, 1)
        plan = state.queue.preview("1", [1, 2])
        assert (plan.to_add, plan.skipped) == ((key(2),), ((key(1), SkipReason.ALREADY_QUEUED),))
        assert len(state.queue.jobs()) == 1

    def test_manual_selection_needs_a_source(self, state):
        with pytest.raises(ValueError):
            enqueue(state, 1, selection_mode=SelectionMode.MANUAL)


@pytest.mark.unit
class TestReorder:
    def test_reorder_with_the_current_revision(self, state):
        a, b, c = enqueue(state, 1, 2, 3).added
        revision = state.queue.queue_revision()
        assert state.queue.reorder([c.id, a.id, b.id], revision) == revision + 1
        assert [j.id for j in state.queue.jobs()] == [c.id, a.id, b.id]

    def test_stale_revision_is_a_conflict(self, state):
        a, b = enqueue(state, 1, 2).added
        revision = state.queue.queue_revision()
        state.queue.reorder([b.id, a.id], revision)
        with pytest.raises(StateConflictError):
            state.queue.reorder([a.id, b.id], revision)
        assert [j.id for j in state.queue.jobs()] == [b.id, a.id]

    def test_list_that_misses_a_job_added_meanwhile_is_a_conflict(self, state):
        a, b = enqueue(state, 1, 2).added
        revision = state.queue.queue_revision()
        enqueue(state, 3)
        with pytest.raises(StateConflictError):
            state.queue.reorder([b.id, a.id], revision)

    def test_new_jobs_go_to_the_end_after_a_reorder(self, state):
        a, b = enqueue(state, 1, 2).added
        state.queue.reorder([b.id, a.id], state.queue.queue_revision())
        (c,) = enqueue(state, 3).added
        assert [j.id for j in state.queue.jobs()] == [b.id, a.id, c.id]


@pytest.mark.unit
class TestManualPauseVersusWaiting:
    def test_pausing_a_queued_job(self, state):
        (job,) = enqueue(state, 1).added
        assert state.queue.pause(job.id).state is JobState.PAUSED
        assert state.queue.resume(job.id).state is JobState.QUEUED

    def test_running_job_only_gets_a_stop_request(self, state):
        claim = _running(state)
        job = state.queue.pause(claim.job.id)
        assert (job.state, job.stop_target, job.attempt_id) == (JobState.STOPPING, StopTarget.PAUSED, claim.attempt_id)

    def test_stopping_job_keeps_its_slot_until_acknowledged(self, state):
        claim = _running(state)
        enqueue(state, 2)
        state.queue.pause(claim.job.id)
        assert state.queue.claim_next(WIFI) is None
        assert state.queue.report_progress(claim.attempt_id, 10) is AttemptStatus.STOP
        assert state.queue.ack_stopped(claim.attempt_id)
        assert state.queue.get(claim.job.id).state is JobState.PAUSED
        assert state.queue.claim_next(WIFI).job.key == key(2)

    def test_network_hold_and_release_on_a_queued_job(self, state):
        (job,) = enqueue(state, 1).added
        assert state.queue.hold(job.id, WaitReason.NETWORK)
        assert (state.queue.get(job.id).state, state.queue.get(job.id).wait_reason) == (JobState.WAITING, WaitReason.NETWORK)
        assert state.queue.release(job.id, WaitReason.NETWORK)
        assert state.queue.get(job.id).state is JobState.QUEUED

    def test_network_return_does_not_lift_a_manual_pause(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.hold(job.id, WaitReason.NETWORK)
        state.queue.pause(job.id)
        assert not state.queue.release(job.id, WaitReason.NETWORK)
        assert state.queue.get(job.id).state is JobState.PAUSED

    def test_network_loss_does_not_turn_a_pause_into_a_wait(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.pause(job.id)
        assert not state.queue.hold(job.id, WaitReason.NETWORK)
        assert state.queue.get(job.id).state is JobState.PAUSED

    def test_pause_outranks_a_pending_network_stop(self, state):
        claim = _running(state)
        assert state.queue.hold(claim.job.id, WaitReason.NETWORK)
        state.queue.pause(claim.job.id)
        assert not state.queue.release(claim.job.id, WaitReason.NETWORK)
        state.queue.ack_stopped(claim.attempt_id)
        assert state.queue.get(claim.job.id).state is JobState.PAUSED

    def test_network_back_before_the_ack_requeues(self, state):
        claim = _running(state)
        state.queue.hold(claim.job.id, WaitReason.NETWORK)
        assert state.queue.release(claim.job.id, WaitReason.NETWORK)
        state.queue.ack_stopped(claim.attempt_id)
        assert state.queue.get(claim.job.id).state is JobState.QUEUED

    def test_running_network_hold_ends_waiting_after_ack(self, state):
        claim = _running(state)
        state.queue.hold(claim.job.id, WaitReason.NETWORK)
        state.queue.ack_stopped(claim.attempt_id)
        job = state.queue.get(claim.job.id)
        assert (job.state, job.wait_reason, job.attempt_id) == (JobState.WAITING, WaitReason.NETWORK, None)

    def test_second_hold_keeps_the_first_reason_while_stopping(self, state):
        claim = _running(state)
        assert state.queue.hold(claim.job.id, WaitReason.INTERACTION)
        assert not state.queue.hold(claim.job.id, WaitReason.NETWORK)
        state.queue.ack_stopped(claim.attempt_id)
        assert state.queue.get(claim.job.id).wait_reason is WaitReason.INTERACTION

    def test_release_of_another_reason_does_nothing(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.hold(job.id, WaitReason.INTERACTION)
        assert not state.queue.release(job.id, WaitReason.NETWORK)

    def test_resume_does_not_lift_a_wait(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.hold(job.id, WaitReason.STORAGE)
        with pytest.raises(InvalidTransitionError):
            state.queue.resume(job.id)

    def test_resume_during_stopping_undoes_only_the_pause(self, state):
        claim = _running(state)
        state.queue.pause(claim.job.id)
        assert state.queue.resume(claim.job.id).stop_target is StopTarget.QUEUED
        state.queue.ack_stopped(claim.attempt_id)
        assert state.queue.get(claim.job.id).state is JobState.QUEUED

    def test_resume_of_a_running_job_is_refused(self, state):
        claim = _running(state)
        with pytest.raises(InvalidTransitionError):
            state.queue.resume(claim.job.id)

    def test_cancel_is_final_while_stopping(self, state):
        claim = _running(state)
        state.queue.cancel(claim.job.id)
        with pytest.raises(InvalidTransitionError):
            state.queue.pause(claim.job.id)
        state.queue.ack_stopped(claim.attempt_id)
        assert state.queue.get(claim.job.id).state is JobState.CANCELLED


@pytest.mark.unit
class TestCellularConsent:
    def test_wifi_only_job_is_not_claimed_on_cellular(self, state):
        enqueue(state, 1)
        assert state.queue.claim_next(CELLULAR) is None

    def test_consent_belongs_to_one_job(self, state):
        a, b = enqueue(state, 1, 2).added
        state.queue.allow_cellular(b.id, True)
        claim = state.queue.claim_next(CELLULAR)
        assert claim.job.id == b.id
        assert state.queue.get(a.id).cellular_allowed is False

    def test_consent_does_not_carry_to_a_new_job(self, state):
        (job,) = enqueue(state, 1).added
        state.queue.allow_cellular(job.id, True)
        (next_job,) = enqueue(state, 2).added
        assert next_job.cellular_allowed is False

    def test_global_policy_applies_without_changing_jobs(self, state):
        (job,) = enqueue(state, 1).added
        state.preferences.update(download_network=DownloadNetwork.ANY)
        assert state.queue.claim_next(CELLULAR).job.id == job.id
        assert state.queue.get(job.id).cellular_allowed is False

    def test_offline_claims_nothing(self, state):
        (job,) = enqueue(state, 1, cellular_allowed=True).added
        assert state.queue.claim_next(NetworkKind.OFFLINE) is None


@pytest.mark.unit
class TestClaim:
    def test_order_and_one_active_download(self, state):
        a, b = enqueue(state, 1, 2).added
        claim = state.queue.claim_next(WIFI)
        assert claim.job.id == a.id and claim.job.attempts == 1
        assert state.queue.claim_next(WIFI) is None

    def test_higher_limit(self, state):
        enqueue(state, 1, 2, 3)
        state.preferences.update(max_active_downloads=2)
        assert state.queue.claim_next(WIFI) and state.queue.claim_next(WIFI)
        assert state.queue.claim_next(WIFI) is None

    def test_waiting_for_interaction_does_not_block_the_rest(self, state):
        a, b = enqueue(state, 1, 2).added
        claim = state.queue.claim_next(WIFI)
        assert state.queue.wait(claim.attempt_id, WaitReason.INTERACTION)
        assert state.queue.claim_next(WIFI).job.id == b.id

    def test_paused_jobs_are_skipped(self, state):
        a, b = enqueue(state, 1, 2).added
        state.queue.pause(a.id)
        assert state.queue.claim_next(WIFI).job.id == b.id


@pytest.mark.unit
class TestAttemptFencing:
    def test_progress_and_partial_are_recorded(self, state):
        claim = _running(state)
        assert state.queue.report_progress(claim.attempt_id, 100, 1000) is AttemptStatus.CONTINUE
        assert state.queue.set_partial(claim.attempt_id, partial(), 120) is AttemptStatus.CONTINUE
        assert state.queue.set_target(claim.attempt_id, StorageLocator("fs", "/v/f.mp4")) is AttemptStatus.CONTINUE
        job = state.queue.get(claim.job.id)
        assert (job.bytes_done, job.bytes_total, job.partial, job.target) == (
            120,
            1000,
            partial(),
            StorageLocator("fs", "/v/f.mp4"),
        )

    def test_progress_after_a_pause_cannot_undo_it(self, state):
        claim = _running(state)
        state.queue.pause(claim.job.id)
        assert state.queue.report_progress(claim.attempt_id, 500) is AttemptStatus.STOP
        assert state.queue.get(claim.job.id).state is JobState.STOPPING

    def test_stop_requested_attempt_cannot_finalize(self, state):
        claim = _running(state)
        state.queue.hold(claim.job.id, WaitReason.NETWORK)
        assert not state.queue.begin_finalize(claim.attempt_id)
        assert state.queue.complete(claim.attempt_id, spec()) is None
        assert state.library.items() == []

    def test_complete_needs_begin_finalize(self, state):
        claim = _running(state)
        assert state.queue.complete(claim.attempt_id, spec()) is None

    def test_finalizing_refuses_pause_and_cancel(self, state):
        claim = _running(state)
        state.queue.begin_finalize(claim.attempt_id)
        for action in (state.queue.pause, state.queue.cancel):
            with pytest.raises(InvalidTransitionError) as info:
                action(claim.job.id)
            assert info.value.code == "state.finalizing"

    def test_unknown_attempt_is_lost(self, state):
        _running(state)
        assert state.queue.report_progress("nope", 1) is AttemptStatus.LOST
        assert not state.queue.ack_stopped("nope")

    def test_executor_wait_during_a_stop_keeps_the_requested_state(self, state):
        claim = _running(state)
        state.queue.pause(claim.job.id)
        assert state.queue.wait(claim.attempt_id, WaitReason.STORAGE, JobError("storage.full"))
        job = state.queue.get(claim.job.id)
        assert (job.state, job.last_error) == (JobState.PAUSED, JobError("storage.full"))

    def test_fail_and_retry(self, state):
        claim = _running(state)
        state.queue.fail(claim.attempt_id, JobError("download.failed", (("status", 403),)))
        failed = state.queue.get(claim.job.id)
        assert (failed.state, failed.last_error, failed.attempt_id) == (
            JobState.FAILED,
            JobError("download.failed", (("status", 403),)),
            None,
        )
        assert state.queue.retry(claim.job.id).state is JobState.QUEUED
        assert state.queue.claim_next(WIFI).job.attempts == 2


@pytest.mark.unit
class TestCompletion:
    def test_complete_is_atomic(self, state):
        claim = _running(state)
        state.queue.begin_finalize(claim.attempt_id)
        with closing(sqlite3.connect(state.database.path)) as conn:
            conn.execute(
                """CREATE TRIGGER fail_completion BEFORE UPDATE OF state ON download_job
                   WHEN NEW.state = 'completed' BEGIN SELECT RAISE(ABORT, 'disk full'); END"""
            )
            conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            state.queue.complete(claim.attempt_id, spec())
        assert state.library.items() == []
        assert state.queue.get(claim.job.id).state is JobState.FINALIZING

    def test_completed_job_is_terminal(self, state):
        job, _ = download(state)
        with pytest.raises(InvalidTransitionError):
            state.queue.pause(job.id)
        with pytest.raises(InvalidTransitionError):
            state.queue.cancel(job.id)


@pytest.mark.unit
class TestSourceDecision:
    def test_same_source_keeps_the_partial(self, state):
        claim = _running(state)
        state.queue.set_partial(claim.attempt_id, partial())
        state.queue.wait(claim.attempt_id, WaitReason.SOURCE_DECISION)
        job = state.queue.choose_source(claim.job.id, SOURCE, VARIANT)
        assert (job.state, job.partial, job.selection_mode) == (JobState.QUEUED, partial(), SelectionMode.MANUAL)

    def test_other_source_drops_the_partial(self, state):
        claim = _running(state)
        state.queue.set_partial(claim.attempt_id, partial(), 500)
        state.queue.wait(claim.attempt_id, WaitReason.SOURCE_DECISION)
        job = state.queue.choose_source(claim.job.id, OTHER_SOURCE, VARIANT)
        assert (job.partial, job.bytes_done, job.source) == (None, None, OTHER_SOURCE)

    def test_restart_drops_the_partial_of_the_same_source(self, state):
        claim = _running(state)
        state.queue.set_partial(claim.attempt_id, partial())
        state.queue.wait(claim.attempt_id, WaitReason.SOURCE_DECISION)
        assert state.queue.choose_source(claim.job.id, SOURCE, VARIANT, restart=True).partial is None

    def test_not_while_an_attempt_runs(self, state):
        claim = _running(state)
        with pytest.raises(InvalidTransitionError):
            state.queue.choose_source(claim.job.id, OTHER_SOURCE)


@pytest.mark.unit
class TestRecovery:
    def test_running_job_is_requeued_and_its_old_attempt_is_lost(self, state):
        claim = _running(state)
        state.queue.set_partial(claim.attempt_id, partial())
        recovery = state.queue.recover_interrupted()
        assert recovery.requeued == (claim.job.id,)
        job = state.queue.get(claim.job.id)
        assert (job.state, job.attempt_id, job.partial, job.last_error) == (
            JobState.QUEUED,
            None,
            partial(),
            JobError(INTERRUPTED),
        )
        assert state.queue.report_progress(claim.attempt_id, 1) is AttemptStatus.LOST
        assert not state.queue.begin_finalize(claim.attempt_id)

    def test_stopping_job_settles_on_its_target(self, state):
        claim = _running(state)
        state.queue.pause(claim.job.id)
        assert state.queue.recover_interrupted().settled == (claim.job.id,)
        assert state.queue.get(claim.job.id).state is JobState.PAUSED

    def test_finalizing_job_goes_to_reconciliation_with_a_new_attempt(self, state):
        claim = _running(state)
        state.queue.set_target(claim.attempt_id, StorageLocator("fs", "/v/f.mp4"))
        state.queue.begin_finalize(claim.attempt_id)
        (reconcile,) = state.queue.recover_interrupted().reconcile
        assert reconcile.job.target == StorageLocator("fs", "/v/f.mp4")
        assert state.queue.complete(claim.attempt_id, spec()) is None
        assert state.queue.complete(reconcile.attempt_id, spec()) is not None


@pytest.mark.unit
class TestNoSecrets:
    def test_urls_are_refused_in_identity_fields(self, state):
        claim = _running(state)
        leaky = partial().__class__(
            locator=StorageLocator("fs", "/v/x.part"),
            source=SOURCE.__class__(**{**SOURCE.__dict__, "source": "https://cdn.example/v.m3u8?token=s3cret"}),
            variant=VARIANT,
        )
        with pytest.raises(ValueError):
            state.queue.set_partial(claim.attempt_id, leaky)
        with pytest.raises(ValueError):
            state.queue.fail(claim.attempt_id, JobError("download.failed", (("url", "https://x/?token=s3cret"),)))

    def test_full_lifecycle_stores_no_stream_url(self, state):
        from alt_ani_core.models import PlayerEntry, StreamProtocol, StreamVariant
        from alt_ani_core.rules.criteria import source_identity, variant_identity

        entry = PlayerEntry("9", "Host", "jp", "pl", source="https://group.example/release?token=s3cret")
        variant = StreamVariant("https://cdn.example/master.m3u8?token=s3cret", StreamProtocol.HLS, {"Cookie": "a=s3cret"})
        state.queue.enqueue(
            ref(),
            rows(1),
            DownloadCriteria(),
            selection_mode=SelectionMode.MANUAL,
            source=source_identity(entry),
            variant=variant_identity(variant),
        )
        claim = state.queue.claim_next(WIFI)
        state.queue.set_partial(claim.attempt_id, partial(source=source_identity(entry)))
        state.queue.begin_finalize(claim.attempt_id)
        state.queue.complete(claim.attempt_id, spec())
        assert "s3cret" not in dump_database(state.database.path)


@pytest.mark.unit
def test_unknown_job(state):
    with pytest.raises(UnknownRecordError) as info:
        state.queue.pause("nope")
    assert info.value.code == "state.unknown_job"
