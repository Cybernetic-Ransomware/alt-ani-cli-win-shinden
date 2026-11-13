"""Runtime per database: one configuration owner, an executor independent of UI handles, honest shutdown."""

import json
import subprocess
import sys
import threading

import pytest

from alt_ani_core.config import AccessConfig
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.storage.fs import FsFileStore

from .conftest import SERIES, V2Client, ep, make_v2


def second_handle(v2, **overrides) -> dict:
    other = V2Client(v2.bridge, v2.transport, None, v2.tmp_path)
    return other.create(push=False, **overrides)


class TestConfigurationOwner:
    def test_second_handle_joins_and_shares_state(self, v2):
        joined = second_handle(v2)
        assert joined["ok"]
        v2.data("preferences_update", changes={"audio": "jp"}, expected_revision=0)
        prefs = json.loads(v2.bridge.call("preferences_get", json.dumps({"engine_id": joined["data"]["engine_id"]})))
        assert prefs["data"]["audio"] == "jp"

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"storage": {"kind": "fs", "root": "elsewhere"}}, "storage"),
            ({"cache_dir": "other-cache"}, "cache_dir"),
            ({"access": {"use_flaresolverr": True, "flaresolverr_url": "http://solver"}}, "access"),
            ({"network": "cellular"}, "network"),
        ],
    )
    def test_conflicting_configuration_is_refused(self, v2, overrides, field):
        if "cache_dir" in overrides:
            overrides["cache_dir"] = str(v2.tmp_path / overrides["cache_dir"])
        error = second_handle(v2, **overrides)["error"]
        assert (error["code"], error["kind"], error["params"]) == ("runtime.config_conflict", "conflict", {"field": field})

    def test_ui_access_does_not_touch_the_executor(self, v2):
        v2.data("executor_start")
        v2.data("set_access", access={"use_flaresolverr": True, "flaresolverr_url": "http://solver"})
        assert v2.sources.base_access == []
        v2.data("executor_set_access", access={"use_flaresolverr": True, "flaresolverr_url": "http://solver"})
        assert v2.sources.base_access == [AccessConfig(flaresolverr_url="http://solver", use_flaresolverr=True)]


class TestShutdown:
    def test_close_with_stuck_io_keeps_the_lock_until_it_ends(self, v2):
        (job,) = v2.enqueue()
        v2.data("executor_start")
        assert v2.transfer.started.acquire(timeout=5)
        first = v2.data("runtime_close", timeout_ms=50)
        assert first == {"ended": False, "executor_ended": False, "delivery_threads_alive": 0, "operations_running": 0}
        db = v2.tmp_path / "history.db"
        assert ExecutorOwnership.held_in_process(db)
        assert second_handle(v2)["error"]["code"] == "runtime.closing"
        v2.transfer.io_done.set()
        again = json.loads(v2.bridge.call("runtime_close", json.dumps({"db_path": str(db), "timeout_ms": 5000})))
        assert again["data"] == {"ended": True, "executor_ended": True, "delivery_threads_alive": 0, "operations_running": 0}
        assert not ExecutorOwnership.held_in_process(db)
        reopened = second_handle(v2, storage={"kind": "fs", "root": str(v2.tmp_path / "library")})
        assert reopened["ok"]

    def test_closing_a_handle_leaves_runtime_and_executor_running(self, v2):
        v2.data("executor_start")
        joined = second_handle(v2)["data"]["engine_id"]
        v2.close()
        status = json.loads(v2.bridge.call("executor_status", json.dumps({"engine_id": joined})))["data"]
        assert (status["state"], status["owner"]) == ("running", "self")

    def test_push_thread_is_reported_when_stuck(self, v2):
        entered, release = threading.Event(), threading.Event()

        def listener(_message: str) -> None:
            entered.set()
            release.wait(5)

        other = V2Client(v2.bridge, v2.transport, None, v2.tmp_path)
        other.create(listener=listener)
        other.data("network_set", kind="cellular")
        assert entered.wait(5)
        result = v2.data("runtime_close", timeout_ms=100)
        assert result["delivery_threads_alive"] == 1 and result["ended"] is False
        release.set()
        db = str(v2.tmp_path / "history.db")
        assert json.loads(v2.bridge.call("runtime_close", json.dumps({"db_path": db})))["data"]["ended"] is True

    def test_close_waits_for_a_running_export(self, v2):
        from .test_library import MemoryTarget

        (item,) = v2.download(1)
        gate = threading.Event()
        target = MemoryTarget(gate)
        handle = json.loads(v2.bridge.register_platform_object(target))["data"]["handle"]
        replies: list[dict] = []
        export = threading.Thread(
            target=lambda: replies.append(
                v2.call("library_export", item_ids=[item["item_id"]], target={"kind": "platform", "handle": handle})
            )
        )
        export.start()
        assert target.parked.wait(5)
        first = v2.data("runtime_close", timeout_ms=100)
        assert (first["ended"], first["operations_running"]) == (False, 1)
        db = str(v2.tmp_path / "history.db")
        assert second_handle(v2)["error"]["code"] == "runtime.closing"
        gate.set()
        export.join(5)
        assert replies[0]["data"]["interrupted"] == "cancelled" and target.published == {}
        again = json.loads(v2.bridge.call("runtime_close", json.dumps({"db_path": db})))["data"]
        assert (again["ended"], again["operations_running"]) == (True, 0)

    def test_a_stuck_listener_is_reported_by_every_close_until_it_ends(self, v2):
        entered, release = threading.Event(), threading.Event()

        def listener(_message: str) -> None:
            entered.set()
            release.wait(10)

        other = V2Client(v2.bridge, v2.transport, None, v2.tmp_path)
        other.create(listener=listener)
        other.data("network_set", kind="cellular")
        assert entered.wait(5)
        assert other.close()["data"]["delivery_ended"] is False
        db = json.dumps({"db_path": str(v2.tmp_path / "history.db"), "timeout_ms": 100})
        for _ in range(2):
            result = json.loads(v2.bridge.call("runtime_close", db))["data"]
            assert (result["ended"], result["delivery_threads_alive"]) == (False, 1)
        release.set()
        assert json.loads(v2.bridge.call("runtime_close", db.replace('"timeout_ms": 100', '"timeout_ms": 5000')))["data"][
            "ended"
        ]

    def test_handle_built_during_close_is_refused_and_cleaned_up(self, tmp_path, monkeypatch):
        from alt_ani_bridge import bridge as bridge_module

        entered, release = threading.Event(), threading.Event()
        engines, pushes = [], []
        real_engine, real_push = bridge_module.Engine, bridge_module.PushAdapter

        def stalled_engine(*args, **kwargs):
            engine = real_engine(*args, **kwargs)
            engines.append(engine)
            entered.set()
            assert release.wait(5)
            return engine

        def recorded_push(*args, **kwargs):
            pushes.append(real_push(*args, **kwargs))
            return pushes[-1]

        monkeypatch.setattr(bridge_module, "Engine", stalled_engine)
        monkeypatch.setattr(bridge_module, "PushAdapter", recorded_push)
        client = make_v2(tmp_path)
        storage = {"kind": "fs", "root": str(tmp_path / "library")}
        replies: list[dict] = []
        creator = threading.Thread(target=lambda: replies.append(client.create(storage=storage)))
        creator.start()
        assert entered.wait(5)
        db = str(tmp_path / "history.db")
        closed = json.loads(client.bridge.call("runtime_close", json.dumps({"db_path": db, "timeout_ms": 5000})))
        assert closed["data"]["ended"]
        release.set()
        creator.join(5)
        (reply,) = replies
        assert reply["error"]["code"] == "runtime.closing"
        assert client.bridge.engine_entries() == []
        assert engines[0].closed
        assert not pushes[0].thread.is_alive()
        assert client.create(storage=storage)["ok"]
        shut = json.loads(client.bridge.shutdown(10))
        assert shut["ok"] and shut["data"]["ended"], shut


class TestPlatformObjects:
    def test_platform_store_is_held_until_its_runtime_closes(self, tmp_path):
        client = make_v2(tmp_path)
        registered = json.loads(client.bridge.register_platform_object(FsFileStore(tmp_path / "lib")))["data"]
        assert registered["kind"] == "file_store"
        assert client.create(push=False, storage={"kind": "platform", "handle": registered["handle"]})["ok"]
        busy = json.loads(client.bridge.release_platform_object(registered["handle"]))["error"]
        assert (busy["code"], busy["kind"]) == ("platform.in_use", "conflict")
        assert client.data("runtime_close")["ended"] is True
        assert json.loads(client.bridge.release_platform_object(registered["handle"]))["ok"]

    def test_wrong_and_unknown_objects(self, v2):
        bad = json.loads(v2.bridge.register_platform_object(object()))["error"]
        assert bad["params"]["problem"] == "unsupported_platform_object"
        error = v2.error("library_export", item_ids=[], target={"kind": "platform", "handle": "nope"})
        assert error["code"] == "unknown_platform_object"


class TestSingleExecutor:
    def test_second_bridge_instance_gets_busy(self, v2, tmp_path):
        v2.data("executor_start")
        other = make_v2(tmp_path)
        assert other.create(push=False, storage={"kind": "fs", "root": str(tmp_path / "library")})["ok"]
        error = other.error("executor_start")
        assert (error["code"], error["kind"]) == ("executor.busy", "conflict")
        assert other.data("executor_status")["owner"] == "other"
        json.loads(other.bridge.shutdown(5))

    def test_other_process_gets_busy(self, v2, tmp_path):
        v2.data("executor_start")
        script = (
            "import sys; from pathlib import Path\n"
            "from alt_ani_core.history.ownership import ExecutorOwnership\n"
            "print(ExecutorOwnership.try_acquire(Path(sys.argv[1])) is None)\n"
        )
        out = subprocess.run([sys.executable, "-c", script, str(tmp_path / "history.db")], capture_output=True, text=True)
        assert out.stdout.strip() == "True"
