import pytest

from alt_ani_core.rules import retry
from alt_ani_core.rules.conditions import (
    STORAGE_RESERVE_BYTES,
    Conditions,
    blocking_condition,
    flaresolverr_allowed,
)
from alt_ani_core.rules.resume import (
    KIND_HLS,
    KIND_HTTP,
    ResumeStep,
    checkpoint_of,
    complete_evidence,
    has_resume_evidence,
    may_discard_partial,
    plan_resume,
    with_checkpoint,
)
from alt_ani_core.state import (
    AlternativesPolicy,
    DownloadCriteria,
    DownloadJob,
    DownloadNetwork,
    EpisodeKey,
    JobState,
    NetworkKind,
    PartialTransfer,
    ResourceValidator,
    SelectionMode,
    SourceIdentity,
    StorageLocator,
    VariantIdentity,
    WaitReason,
)

SOURCE = SourceIdentity("1", "host", "jp", "pl", "1080p", "", "")
OTHER = SourceIdentity("2", "other", "jp", "pl", "1080p", "", "")
VARIANT = VariantIdentity("720p", 720, "hls")
HTTP_OK = ResourceValidator(KIND_HTTP, (("etag", "e" * 64), ("total", 100), ("length", 50)))
HLS_OK = ResourceValidator(
    KIND_HLS, (("manifest", "m" * 64), ("playlist", "p"), ("segments", 4), ("length", 10), ("fragments", 1))
)


def _partial(validator) -> PartialTransfer:
    return PartialTransfer(StorageLocator("fs", ".work/j/p/"), SOURCE, VARIANT, validator)


def _job(**changes) -> DownloadJob:
    base = {
        "id": "j",
        "key": EpisodeKey("1", 1),
        "seq": 1,
        "queue_pos": 1,
        "state": JobState.QUEUED,
        "criteria": DownloadCriteria(),
        "alternatives": AlternativesPolicy.OFF,
        "selection_mode": SelectionMode.AUTO,
        "created_at": "t",
        "updated_at": "t",
    }
    return DownloadJob(**(base | changes))


@pytest.mark.unit
class TestResumePlan:
    def test_no_partial_is_a_fresh_transfer(self):
        assert plan_resume(None, SOURCE, VARIANT).step is ResumeStep.FRESH

    @pytest.mark.parametrize("validator", [HTTP_OK, HLS_OK])
    def test_evidence_and_checkpoint_allow_a_resume(self, validator):
        assert plan_resume(_partial(validator), SOURCE, VARIANT).step is ResumeStep.RESUME

    @pytest.mark.parametrize(
        "validator",
        [
            ResourceValidator(KIND_HTTP, (("total", 100), ("length", 50))),
            ResourceValidator(KIND_HLS, (("manifest", ""), ("length", 10), ("fragments", 1))),
            ResourceValidator(KIND_HLS, (("manifest", "m"), ("length", 10), ("fragments", 0))),
            ResourceValidator("dash", (("length", 10),)),
        ],
    )
    def test_durable_bytes_without_evidence_cannot_be_resumed(self, validator):
        plan = plan_resume(_partial(validator), SOURCE, VARIANT)
        assert (plan.step, plan.reason) == (ResumeStep.IMPOSSIBLE, "no_validator")

    @pytest.mark.parametrize("validator", [None, ResourceValidator(KIND_HTTP, (("etag", "e"), ("total", 100), ("length", 0)))])
    def test_a_partial_without_durable_bytes_starts_over_freely(self, validator):
        assert plan_resume(_partial(validator), OTHER, VARIANT).step is ResumeStep.FRESH

    def test_another_source_or_variant_is_impossible(self):
        assert plan_resume(_partial(HTTP_OK), OTHER, VARIANT).reason == "different_source"
        assert plan_resume(_partial(HTTP_OK), SOURCE, VariantIdentity("1080p", 1080, "hls")).step is ResumeStep.IMPOSSIBLE

    def test_complete_evidence_only_needs_finalizing(self):
        plan = plan_resume(_partial(complete_evidence(10, "a" * 64)), OTHER, VARIANT)
        assert plan.step is ResumeStep.FINALIZE

    def test_checkpoint_replaces_progress_only(self):
        updated = with_checkpoint(HLS_OK, 99, 3)
        assert checkpoint_of(updated) == (99, 3)
        assert has_resume_evidence(updated)
        assert dict(updated.fields)["manifest"] == "m" * 64

    @pytest.mark.parametrize(
        ("policy", "mode", "allowed"),
        [
            (AlternativesPolicy.OFF, SelectionMode.AUTO, False),
            (AlternativesPolicy.COMPATIBLE, SelectionMode.AUTO, True),
            (AlternativesPolicy.COMPATIBLE, SelectionMode.MANUAL, False),
        ],
    )
    def test_only_compatible_auto_jobs_may_start_over_unasked(self, policy, mode, allowed):
        assert may_discard_partial(_job(alternatives=policy, selection_mode=mode)) is allowed


@pytest.mark.unit
class TestRetry:
    def test_transient_errors_back_off_then_fail(self):
        outcomes = [retry.transfer_outcome("timeout", None, network_allowed=True, retry_count=n) for n in range(4)]
        assert [o.action for o in outcomes] == [retry.Action.DEFER] * 3 + [retry.Action.FAIL]
        assert [o.delay_sec for o in outcomes[:3]] == list(retry.BACKOFF_SEC)

    def test_full_disk_waits_for_storage(self):
        outcome = retry.transfer_outcome("storage.no_space", None, network_allowed=True, retry_count=0)
        assert (outcome.action, outcome.reason) == (retry.Action.WAIT, WaitReason.STORAGE)

    def test_error_while_the_network_is_not_allowed_waits_without_spending_a_retry(self):
        outcome = retry.transfer_outcome("network_error", None, network_allowed=False, retry_count=5)
        assert (outcome.action, outcome.reason, outcome.counts) == (retry.Action.WAIT, WaitReason.NETWORK, False)

    def test_access_denied_fails_at_once(self):
        assert (
            retry.transfer_outcome("storage.access_denied", None, network_allowed=True, retry_count=0).action
            is retry.Action.FAIL
        )

    def test_wait_parameters_may_be_named_like_the_arguments(self):
        outcome = retry.wait(WaitReason.SOURCE_DECISION, "resume.impossible", reason="no_validator", code="x")
        assert dict(outcome.error.params) == {"reason": "no_validator", "code": "x"}

    def test_retry_after_lengthens_the_backoff(self):
        assert retry.transient(0, "x", retry_after=900).delay_sec == 900


@pytest.mark.unit
class TestConditions:
    def _conditions(self, network=NetworkKind.WIFI, policy=DownloadNetwork.WIFI_ONLY, free=None, notified=False):
        return Conditions(network, policy, free, notified)

    @pytest.mark.parametrize(
        ("network", "policy", "consent", "expected"),
        [
            (NetworkKind.WIFI, DownloadNetwork.WIFI_ONLY, False, None),
            (NetworkKind.CELLULAR, DownloadNetwork.WIFI_ONLY, False, WaitReason.NETWORK),
            (NetworkKind.CELLULAR, DownloadNetwork.WIFI_ONLY, True, None),
            (NetworkKind.CELLULAR, DownloadNetwork.ANY, False, None),
            (NetworkKind.OFFLINE, DownloadNetwork.ANY, True, WaitReason.NETWORK),
        ],
    )
    def test_network(self, network, policy, consent, expected):
        job = _job(cellular_allowed=consent)
        assert blocking_condition(job, self._conditions(network, policy), storage_blocked=False) is expected

    def test_network_is_checked_before_storage(self):
        job = _job()
        assert blocking_condition(job, self._conditions(NetworkKind.OFFLINE), storage_blocked=True) is WaitReason.NETWORK

    def test_storage_stays_blocked_without_evidence(self):
        job = _job(bytes_total=None)
        assert blocking_condition(job, self._conditions(free=10**12), storage_blocked=True) is WaitReason.STORAGE

    def test_storage_is_released_by_enough_space_for_the_remainder(self):
        job = _job(bytes_total=1000, bytes_done=400)
        enough = 600 + STORAGE_RESERVE_BYTES
        assert blocking_condition(job, self._conditions(free=enough), storage_blocked=True) is None
        assert blocking_condition(job, self._conditions(free=enough - 1), storage_blocked=True) is WaitReason.STORAGE

    def test_storage_is_released_by_a_platform_notice(self):
        assert blocking_condition(_job(), self._conditions(notified=True), storage_blocked=True) is None

    @pytest.mark.parametrize(
        ("configured", "network", "expected"),
        [(True, NetworkKind.WIFI, True), (True, NetworkKind.CELLULAR, False), (False, NetworkKind.WIFI, False)],
    )
    def test_flaresolverr_needs_both_the_configuration_and_wifi(self, configured, network, expected):
        assert flaresolverr_allowed(configured, network) is expected
