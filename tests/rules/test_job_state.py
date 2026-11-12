import pytest

from alt_ani_core.errors import InvalidTransitionError
from alt_ani_core.rules.job_state import TRANSITIONS, check_transition, merge_stop
from alt_ani_core.state import JobState, StopTarget


@pytest.mark.unit
class TestTransitions:
    def test_every_state_has_an_entry(self):
        assert set(TRANSITIONS) == set(JobState)

    @pytest.mark.parametrize("state", [JobState.COMPLETED, JobState.CANCELLED])
    def test_terminal_states_go_nowhere(self, state):
        assert TRANSITIONS[state] == frozenset()

    def test_a_running_job_cannot_be_paused_without_stopping_first(self):
        with pytest.raises(InvalidTransitionError) as info:
            check_transition("j", JobState.RUNNING, JobState.PAUSED)
        assert info.value.code == "state.invalid_transition"

    def test_a_stopping_job_cannot_be_claimed_again(self):
        assert JobState.RUNNING not in TRANSITIONS[JobState.STOPPING]

    def test_only_finalizing_reaches_completed(self):
        assert [s for s, targets in TRANSITIONS.items() if JobState.COMPLETED in targets] == [JobState.FINALIZING]

    def test_finalizing_refusal_has_its_own_code(self):
        with pytest.raises(InvalidTransitionError) as info:
            check_transition("j", JobState.FINALIZING, JobState.PAUSED)
        assert info.value.code == "state.finalizing"

    def test_a_paused_job_is_never_moved_to_waiting(self):
        assert JobState.WAITING not in TRANSITIONS[JobState.PAUSED]


@pytest.mark.unit
class TestMergeStop:
    @pytest.mark.parametrize(
        "current,requested,expected",
        [
            (StopTarget.CANCELLED, StopTarget.PAUSED, None),
            (StopTarget.CANCELLED, StopTarget.QUEUED, None),
            (StopTarget.CANCELLED, StopTarget.CANCELLED, StopTarget.CANCELLED),
            (StopTarget.PAUSED, StopTarget.CANCELLED, StopTarget.CANCELLED),
            (StopTarget.WAITING, StopTarget.PAUSED, StopTarget.PAUSED),
            (StopTarget.PAUSED, StopTarget.WAITING, None),
            (StopTarget.PAUSED, StopTarget.QUEUED, StopTarget.QUEUED),
            (StopTarget.WAITING, StopTarget.QUEUED, None),
            (StopTarget.QUEUED, StopTarget.WAITING, StopTarget.WAITING),
        ],
    )
    def test_matrix(self, current, requested, expected):
        assert merge_stop(current, requested) is expected
