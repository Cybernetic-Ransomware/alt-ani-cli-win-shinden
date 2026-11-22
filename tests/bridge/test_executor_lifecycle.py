"""Executor lifecycle through the contract; synchronisation by events, never by sleeps."""

import json
import subprocess
import sys
import threading
from unittest.mock import patch

from alt_ani_core.errors import OperationCancelled
from alt_ani_core.history.ownership import ExecutorOwnership

from .conftest import V2Client


class TestStartStop:
    def test_start_is_idempotent_and_status_reports_it(self, v2):
        first = v2.data("executor_start")
        assert (first["state"], first["already_running"]) == ("running", False)
        assert first["recovery"]["counts"]["requeued"] == 0
        assert v2.data("executor_start")["already_running"] is True
        assert v2.data("executor_status") == {"state": "running", "stuck_jobs": [], "owner": "self"}
        assert v2.data("executor_stop", timeout_ms=5000) == {"ended": True, "state": "stopped", "stuck_jobs": []}
        assert v2.data("executor_status")["owner"] == "unknown"

    def test_cancelled_start_releases_ownership(self, v2):
        entered = threading.Event()

        def blocking_recover(state, store, owner, events, cancel):
            entered.set()
            while not cancel.cancelled:
                threading.Event().wait(0.01)
            raise OperationCancelled()

        replies: list = []
        with patch("alt_ani_core.services.offline.recover", blocking_recover):
            thread = threading.Thread(target=lambda: replies.append(v2.call("executor_start", op_id="start-1")))
            thread.start()
            assert entered.wait(5)
            assert v2.bridge.cancel("start-1")
            thread.join(5)
        assert replies[0]["error"]["code"] == "cancelled"
        assert not ExecutorOwnership.held_in_process(v2.tmp_path / "history.db")
        assert v2.data("executor_start")["state"] == "running"

    def test_stuck_attempt(self, v2):
        (job,) = v2.enqueue()
        v2.data("executor_start")
        assert v2.transfer.started.acquire(timeout=5)
        stopped = v2.data("executor_stop", timeout_ms=50)
        assert stopped == {"ended": False, "state": "closing", "stuck_jobs": [job["job_id"]]}
        assert v2.error("executor_start")["code"] == "executor.closing"
        v2.transfer.io_done.set()
        assert v2.data("executor_stop", timeout_ms=5000)["ended"] is True

    def test_storage_is_required(self, tmp_path):
        from .conftest import make_v2

        client = make_v2(tmp_path)
        client.create(push=False)
        error = client.error("executor_start")
        assert (error["code"], error["kind"]) == ("storage.not_configured", "storage")
        assert client.error("library_delete", item_ids=["x"])["code"] == "storage.not_configured"
        client.bridge.shutdown(5)


class TestStatusDoesNotContend:
    def test_status_never_takes_the_lock(self, v2):
        with (
            patch.object(ExecutorOwnership, "try_acquire", side_effect=AssertionError("status took the lock")),
            patch("alt_ani_core.history.ownership._try_lock", side_effect=AssertionError("status took the lock")),
        ):
            assert v2.data("executor_status") == {"state": "stopped", "stuck_jobs": [], "owner": "unknown"}

    def test_status_reads_during_a_start_in_another_process_do_not_make_it_busy(self, v2, tmp_path):
        script = (
            "import sys; from pathlib import Path\n"
            "from alt_ani_core.history.ownership import ExecutorOwnership\n"
            "print('ready', flush=True); sys.stdin.readline()\n"
            "owner = ExecutorOwnership.try_acquire(Path(sys.argv[1]))\n"
            "print('busy' if owner is None else 'acquired', flush=True)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path / "history.db")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                v2.data("executor_status")

        reader = threading.Thread(target=hammer)
        try:
            assert child.stdout.readline().strip() == "ready"
            reader.start()
            child.stdin.write("go\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "acquired"
        finally:
            stop.set()
            reader.join(5)
            child.stdin.close()
            child.wait(5)


class TestIndependence:
    def test_closing_the_handle_does_not_stop_the_transfer(self, v2):
        joined = V2Client(v2.bridge, v2.transport, None, v2.tmp_path)
        joined.create(push=False)
        (job,) = v2.enqueue()
        v2.data("executor_start")
        assert v2.transfer.started.acquire(timeout=5)
        v2.close()
        v2.transfer.release.set()
        assert joined.wait_job(job["job_id"], "completed")["state"] == "completed"

    def test_other_process_owner(self, v2, tmp_path):
        script = (
            "import sys; from pathlib import Path\n"
            "from alt_ani_core.history.ownership import ExecutorOwnership\n"
            "owner = ExecutorOwnership.try_acquire(Path(sys.argv[1]))\n"
            "print('held' if owner else 'busy', flush=True)\n"
            "sys.stdin.read()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path / "history.db")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "held"
            # the status read cannot see the other process without contending, so it does not pretend to
            assert v2.data("executor_status")["owner"] == "unknown"
            assert v2.error("executor_start")["code"] == "executor.busy"
        finally:
            child.stdin.close()
            child.wait(5)
        assert v2.data("executor_start")["state"] == "running"
