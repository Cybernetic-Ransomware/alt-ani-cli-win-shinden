"""Events are refresh signals: immutable once numbered, coalesced per pass, resynchronised by head -> snapshot -> cursor."""

import json
import subprocess
import sys
import threading

from .conftest import SERIES, V2Client, ep, make_v2


def changed(events, type_: str) -> list[dict]:
    return [e for e in events if e["type"] == type_]


class TestCursor:
    def test_head_cursor_and_epoch(self, v2):
        head = v2.head()
        events, cursor = v2.drain(head)
        assert events == [] and cursor == head
        reply = v2.data("events_next", cursor={"epoch": "old-epoch", "seq": 1})
        assert reply["resync"] is True and reply["epoch"] == head["epoch"]

    def test_published_events_do_not_change(self, v2):
        head = v2.head()
        v2.data("network_set", kind="cellular")
        first, _ = v2.drain(head)
        first[0]["data"]["kind"] = "tampered"
        again, _ = v2.drain(head)
        assert again[0]["data"]["kind"] == "cellular" and again[0]["seq"] == first[0]["seq"]

    def test_overflow_requires_resync(self, tmp_path):
        client = make_v2(tmp_path, journal_size=4)
        client.create(push=False)
        head = client.head()
        for kind in ("cellular", "wifi") * 3:
            client.data("network_set", kind=kind)
        reply = client.data("events_next", cursor=head)
        assert reply["resync"] is True and reply["events"] == []
        client.bridge.shutdown(5)

    def test_waiting_poll_is_cancelled(self, v2):
        replies: list = []
        thread = threading.Thread(
            target=lambda: replies.append(v2.call("events_next", cursor=v2.head(), wait_ms=30000, op_id="poll-1"))
        )
        thread.start()
        for _ in range(500):
            if v2.bridge.cancel("poll-1"):
                break
            threading.Event().wait(0.01)
        thread.join(5)
        assert not thread.is_alive() and replies[0]["error"]["code"] == "cancelled"


class TestSignals:
    def test_changes_between_passes_coalesce_to_one_signal_per_entity(self, v2):
        (job,) = v2.enqueue()
        head = v2.head()
        revision = job["revision"]
        for action in ("queue_pause", "queue_resume", "queue_pause"):
            revision = v2.data(action, job_id=job["job_id"], expected_revision=revision)["job"]["revision"]
        event, _ = v2.wait_event(head, lambda e: e["type"] == "job.changed" and e["data"]["revision"] == revision)
        events, _ = v2.drain(head)
        jobs = changed(events, "job.changed")
        assert all(e["data"]["revision"] <= revision for e in jobs)
        assert jobs[-1]["data"] == {
            "job_id": job["job_id"],
            "revision": revision,
            "state": "paused",
            "wait_reason": None,
            "bytes_done": None,
            "bytes_total": None,
        }

    def test_progress_arrives_as_job_changed_with_bytes(self, v2):
        (job,) = v2.enqueue()
        head = v2.head()
        v2.data("executor_start")
        event, _ = v2.wait_event(head, lambda e: e["type"] == "job.changed" and e["data"]["bytes_done"] is not None)
        assert event["data"]["job_id"] == job["job_id"]

    def test_removed_items_are_signalled(self, v2):
        (item,) = v2.download(1)
        head = v2.head()
        v2.data("library_delete", item_ids=[item["item_id"]])
        event, _ = v2.wait_event(head, lambda e: e["type"] == "library.removed")
        assert event["data"]["item_id"] == item["item_id"]

    def test_preferences_and_queue_signals(self, v2):
        head = v2.head()
        v2.data("preferences_update", changes={"audio": "jp"}, expected_revision=0)
        v2.enqueue()
        _, cursor = v2.wait_event(head, lambda e: e["type"] == "preferences.changed" and e["data"]["revision"] == 1)
        v2.wait_event(head, lambda e: e["type"] == "queue.changed")

    def test_changes_from_another_process(self, v2, tmp_path):
        (job,) = v2.enqueue()
        head = v2.head()
        script = (
            "import sys; from pathlib import Path\n"
            "from alt_ani_core.history.local import open_local_state\n"
            "open_local_state(Path(sys.argv[1])).queue.pause(sys.argv[2])\n"
        )
        subprocess.run([sys.executable, "-c", script, str(tmp_path / "history.db"), job["job_id"]], check=True)
        event, _ = v2.wait_event(head, lambda e: e["type"] == "job.changed" and e["data"]["state"] == "paused")
        assert event["data"]["revision"] > job["revision"]

    def test_op_id_correlates_engine_events(self, v2):
        head = v2.head()
        v2.data("network_set", kind="cellular", op_id="net-1")
        events, _ = v2.drain(head)
        assert changed(events, "network.changed")[0]["op_id"] == "net-1"


class TestResyncProcedure:
    def test_snapshot_then_cursor_misses_nothing(self, v2):
        jobs = v2.enqueue((1, 2))
        head = v2.head()
        snapshot = {j["job_id"]: j["revision"] for j in v2.data("queue_snapshot")["jobs"]}
        paused = v2.data("queue_pause", job_id=jobs[1]["job_id"], expected_revision=jobs[1]["revision"])["job"]
        known = dict(snapshot)
        cursor = head
        while known[jobs[1]["job_id"]] < paused["revision"]:
            events, cursor = v2.drain(cursor, wait_ms=200)
            for event in changed(events, "job.changed"):
                if event["data"]["revision"] > known.get(event["data"]["job_id"], -1):
                    known[event["data"]["job_id"]] = v2.job(event["data"]["job_id"])["revision"]
        assert known[jobs[0]["job_id"]] == snapshot[jobs[0]["job_id"]]

    def test_reconnect_after_restart_changes_epoch(self, v2, tmp_path):
        head = v2.head()
        json.loads(v2.bridge.shutdown(5))
        fresh = make_v2(tmp_path)
        fresh.create(push=False)
        assert fresh.data("events_next", cursor=head)["resync"] is True
        fresh.bridge.shutdown(5)


class TestPush:
    def test_blocked_listener_does_not_block_calls_or_the_executor(self, v2):
        entered, release = threading.Event(), threading.Event()

        def listener(_message: str) -> None:
            entered.set()
            release.wait(10)

        pushed = V2Client(v2.bridge, v2.transport, None, v2.tmp_path)
        pushed.create(listener=listener)
        (job,) = v2.enqueue()
        v2.data("network_set", kind="cellular")
        assert entered.wait(5)
        v2.data("network_set", kind="wifi")
        v2.transfer.release.set()
        v2.data("executor_start")
        assert v2.wait_job(job["job_id"], "completed")["state"] == "completed"
        release.set()

    def test_overflowing_listener_gets_one_resync(self, tmp_path):
        client = make_v2(tmp_path, journal_size=4)
        client.create(push=False)
        entered, release = threading.Event(), threading.Event()
        received: list[dict] = []

        def listener(message: str) -> None:
            received.append(json.loads(message))
            entered.set()
            release.wait(10)

        pushed = V2Client(client.bridge, client.transport, None, tmp_path)
        pushed.create(listener=listener)
        client.data("network_set", kind="cellular")
        assert entered.wait(5)
        for kind in ("wifi", "cellular") * 4:
            client.data("network_set", kind=kind)
        release.set()
        for _ in range(500):
            if any(e["type"] == "events.resync_required" for e in received):
                break
            threading.Event().wait(0.01)
        assert [e["type"] for e in received].count("events.resync_required") == 1
        client.bridge.shutdown(5)
