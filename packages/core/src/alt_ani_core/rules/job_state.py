"""Download job state machine; the store applies it inside its transactions, the executor follows its answers."""

from alt_ani_core.errors import InvalidTransitionError
from alt_ani_core.state import ACTIVE_STATES, TERMINAL_STATES, DownloadJob, JobAction, JobState, StopTarget, WaitReason

TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.PAUSED, JobState.WAITING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.STOPPING, JobState.FINALIZING, JobState.WAITING, JobState.FAILED, JobState.QUEUED}),
    JobState.STOPPING: frozenset({JobState.PAUSED, JobState.CANCELLED, JobState.WAITING, JobState.QUEUED}),
    # waiting: publishing the file hit a condition such as a full disk; the target stays recorded
    JobState.FINALIZING: frozenset({JobState.COMPLETED, JobState.FAILED, JobState.WAITING}),
    JobState.PAUSED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.WAITING: frozenset({JobState.QUEUED, JobState.PAUSED, JobState.CANCELLED}),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def invalid(job_id: str, state: JobState, requested: str) -> InvalidTransitionError:
    if state is JobState.FINALIZING:
        return InvalidTransitionError(f"job {job_id} is finalizing its file").coded(
            "state.finalizing", job_id=job_id, requested=requested
        )
    return InvalidTransitionError(f"job {job_id} cannot go from {state} to {requested}").coded(
        "state.invalid_transition", job_id=job_id, state=state.value, requested=requested
    )


def check_transition(job_id: str, state: JobState, target: JobState) -> None:
    if target not in TRANSITIONS[state]:
        raise invalid(job_id, state, target.value)


def merge_stop(current: StopTarget, requested: StopTarget) -> StopTarget | None:
    """None leaves the target unchanged: cancelling is final, a user pause outranks a wait, resume undoes only a pause."""
    if current is StopTarget.CANCELLED:
        return StopTarget.CANCELLED if requested is StopTarget.CANCELLED else None
    if requested in (StopTarget.CANCELLED, StopTarget.PAUSED):
        return requested
    if requested is StopTarget.QUEUED:
        return StopTarget.QUEUED if current is StopTarget.PAUSED else None
    return StopTarget.WAITING if current in (StopTarget.WAITING, StopTarget.QUEUED) else None


def available_actions(job: DownloadJob) -> frozenset[JobAction]:
    """What a user action would change now; mirrors the store's checks, so an offered action is never refused for state."""
    state, stop = job.state, job.stop_target
    actions: set[JobAction] = set()
    stopping_open = state is JobState.STOPPING and stop is not StopTarget.CANCELLED
    if state in (JobState.QUEUED, JobState.WAITING, JobState.RUNNING) or (stopping_open and stop is not StopTarget.PAUSED):
        actions.add(JobAction.PAUSE)
    if state is JobState.PAUSED or (state is JobState.STOPPING and stop is StopTarget.PAUSED):
        actions.add(JobAction.RESUME)
    if state in (JobState.QUEUED, JobState.PAUSED, JobState.WAITING, JobState.FAILED, JobState.RUNNING) or stopping_open:
        actions.add(JobAction.CANCEL)
    if state is JobState.FAILED:
        actions.add(JobAction.RETRY)
    if state not in TERMINAL_STATES:
        actions.add(JobAction.ALLOW_CELLULAR)
    if state not in ACTIVE_STATES and state not in TERMINAL_STATES:
        actions.add(JobAction.CHOOSE_SOURCE)
    if state is JobState.WAITING and job.wait_reason is WaitReason.INTERACTION:
        actions.add(JobAction.RETRY_INTERACTION)
    return frozenset(actions)
