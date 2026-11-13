"""Executor lifecycle; it runs independently of UI handles and sessions, one per database across processes."""

from typing import Any

from alt_ani_bridge.context import Call
from alt_ani_bridge.errors import BridgeError
from alt_ani_bridge.methods import common, method
from alt_ani_core.config import AccessConfig
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.services.offline import ExecutorState


def _publish_state(c: Call) -> dict[str, Any]:
    status = executor_status(c)
    c.publish("executor.state", {"state": status["state"]})
    return status


def executor_start(c: Call) -> Any:
    """Idempotent within the runtime; cancelling during recovery releases ownership and leaves nothing claimed."""
    runtime = c.runtime
    with runtime.lock:
        if runtime.closing:
            raise BridgeError("runtime.closing", "the runtime is closing")
        queue = c.bridge.ensure_queue(runtime)
    if queue.running and queue.status().state is ExecutorState.RUNNING:
        return {"state": "running", "already_running": True, "recovery": None}
    try:
        report = queue.start(cancel=c.token)
    except RuntimeError:
        # another call of this runtime started it meanwhile
        return {"state": "running", "already_running": True, "recovery": None}
    _publish_state(c)
    runtime.changed()
    return {
        "state": "running",
        "already_running": False,
        "recovery": {
            "counts": {
                "requeued": len(report.requeued),
                "settled": len(report.settled),
                "completed": len(report.completed),
                "failed": len(report.failed),
                "marked_missing": len(report.marked_missing),
                "restored": len(report.restored),
                "deletes_finished": len(report.deletes_finished),
                "removals": report.removals,
            },
            "ids": {
                "requeued": list(report.requeued),
                "settled": list(report.settled),
                "completed": list(report.completed),
                "failed": list(report.failed),
                "marked_missing": list(report.marked_missing),
                "restored": list(report.restored),
            },
            "orphans": len(report.orphans),
        },
    }


def executor_status(c: Call) -> dict[str, Any]:
    queue = c.runtime.queue
    status = queue.status() if queue is not None else None
    if status is not None and status.state is not ExecutorState.STOPPED:
        return {"state": status.state.value, "stuck_jobs": list(status.stuck_jobs), "owner": "self"}
    # never contends for the OS lock: a status read must not make a concurrent start fail with executor.busy
    other = ExecutorOwnership.held_in_process(c.runtime.state.database.path)
    return {"state": "stopped", "stuck_jobs": [], "owner": "other" if other else "unknown"}


def executor_stop(c: Call) -> Any:
    """``ended`` false: some I/O still runs, ownership is kept and the state is ``closing``; call again to wait."""
    timeout_ms = c.r.integer("timeout_ms", 10000, minimum=0, maximum=600000)
    queue = c.runtime.queue
    ended = True if queue is None else queue.close(timeout_ms / 1000)
    status = _publish_state(c)
    return {"ended": ended, "state": status["state"], "stuck_jobs": status["stuck_jobs"]}


def executor_set_access(c: Call) -> Any:
    """Only the executor's access; UI handles keep theirs. Applied with the Wi-Fi-only solver rule."""
    access = common.access(c.r) or AccessConfig()
    runtime = c.runtime
    with runtime.lock:
        runtime.executor_access = access
        queue = runtime.queue
    setter = getattr(queue.sources, "set_base_access", None) if queue is not None else None
    if setter is not None:
        setter(access)
    return {"applied_to_running_executor": setter is not None and queue is not None and queue.running}


METHODS = {
    "executor_start": method(executor_start),
    "executor_status": method(executor_status),
    "executor_stop": method(executor_stop, "timeout_ms"),
    "executor_set_access": method(executor_set_access, "access"),
}
