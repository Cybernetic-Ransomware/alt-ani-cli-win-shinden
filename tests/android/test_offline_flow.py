"""Download, library and recovery through the Android host and its production store; mirrored by the device tests."""

import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

from tools.android.controlled_server import MEDIA, Scenario

from .flow import Flow, wait_until

pytestmark = pytest.mark.loopback

VIDEO = (MEDIA / "video.mp4").read_bytes()
# held past Python's 128 KiB file buffer (yt-dlp reads in doubling blocks), so the partial on disk is never empty
PAD = 1024 * 1024 - len(VIDEO)
STALL_AT = 300_000


def _held_scenario() -> Scenario:
    return Scenario(kind="progressive", etag=True, stall_files={"video.mp4": STALL_AT}, stall_skip=1, pad_bytes=PAD)


def _release_after_the_monitor(server) -> None:
    """Actions reach the attempt at the next 0.25 s monitor pass; releasing earlier would let the transfer finish."""
    time.sleep(1.0)
    server.state.release()


def _requests(server) -> list[dict]:
    return server.state.snapshot()["requests"]


def _held(server, root: Path) -> bool:
    """The download request is held and yt-dlp has already written its first bytes to the partial."""
    return any(e.get("stalled") for e in _requests(server)) and any(p.stat().st_size for p in root.rglob("*.part"))


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _work_entries(root: Path) -> list[str]:
    work = root / ".work"
    return sorted(p.relative_to(work).as_posix() for p in work.rglob("*")) if work.is_dir() else []


def _download(flow: Flow, server, scenario: Scenario | None = None) -> tuple[dict, dict, str]:
    server.state.reset(scenario or Scenario(kind="progressive", etag=True))
    series, episode, job_id = flow.enqueue()
    assert flow.data("executor_start")["already_running"] is False
    flow.wait_job(job_id, "completed")
    return series, episode, job_id


def _interrupt(flow: Flow, server, scenario: Scenario | None = None) -> tuple[dict, dict, str]:
    """Starts a transfer the server holds after ``STALL_AT`` bytes, stops the executor, lets the I/O end."""
    server.state.reset(scenario or _held_scenario())
    series, episode, job_id = flow.enqueue()
    flow.data("executor_start")
    wait_until(lambda: _held(server, flow.root))
    stopped = flow.data("executor_stop", timeout_ms=200)
    assert (stopped["ended"], stopped["state"]) == (False, "closing"), stopped
    assert flow.error("executor_start")["code"] == "executor.closing"
    server.state.release()
    assert flow.data("executor_stop", timeout_ms=10000)["ended"] is True
    return series, episode, job_id


@pytest.fixture
def flow(server, tmp_path):
    flow = Flow(server, tmp_path, "fs")
    yield flow
    server.state.release()
    flow.bridge.shutdown(10)


class TestDownloadToPlayback:
    def test_a_download_is_verified_and_plays_locally_with_the_network_gone(self, flow, server):
        series, episode, _ = _download(flow, server)
        (item,) = flow.data("library_list")["items"]
        assert item["size_bytes"] == len(VIDEO)
        (verified,) = flow.data("library_verify", deep=True)["results"]
        assert verified["result"] == "ok", verified
        local = flow.local_target(series, episode)
        path = Path(local["target"]["value"])
        assert (local["mode"], local["target"]["kind"]) == ("local", "file")
        assert path.is_relative_to(flow.root) and ".work" not in path.parts
        assert path.read_bytes() == VIDEO
        wait_until(lambda: not [e for e in _work_entries(flow.root) if "/" in e])
        flow.data("network_set", kind="offline")
        flow.server.state.expire_issued()
        before = len(_requests(server))
        assert flow.local_target(series, episode)["mode"] == "local"
        assert len(_requests(server)) == before

    def test_a_restart_keeps_the_item_and_the_next_start_tidies_the_empty_job_directory(self, flow, server, tmp_path):
        series, episode, job_id = _download(flow, server)
        wait_until(lambda: _work_entries(flow.root) == [job_id])
        flow.shutdown()
        again = Flow(server, tmp_path, "fs")
        try:
            report = again.data("executor_start")["recovery"]
            assert report["orphans"] == 0
            assert _work_entries(again.root) == []
            assert again.local_target(series, episode)["mode"] == "local"
        finally:
            again.shutdown()


class TestInterruptedTransfers:
    def test_a_stop_with_held_io_ends_false_and_keeps_ownership_until_the_io_ends(self, flow, server):
        _interrupt(flow, server)
        assert flow.data("executor_start")["already_running"] is False

    def test_an_interrupted_transfer_resumes_from_its_checkpoint_with_a_matching_validator(self, flow, server, tmp_path):
        _, _, job_id = _interrupt(flow, server)
        done = flow.job(job_id)["bytes_done"]
        assert 0 < done < len(VIDEO) + PAD
        flow.shutdown()
        server.state.reset(Scenario(kind="progressive", etag=True, pad_bytes=PAD))
        again = Flow(server, tmp_path, "fs")
        try:
            again.data("executor_start")
            again.wait_job(job_id, "completed")
            calls = [(e["method"], e["range"]) for e in _requests(server) if e["file"] == "video.mp4"]
            assert ("HEAD", None) in calls
            ranged = [r for m, r in calls if m == "GET" and r]
            assert ranged and all(r != "bytes=0-" for r in ranged), calls
            (item,) = again.data("library_list")["items"]
            assert again.data("library_verify", deep=True)["results"][0]["result"] == "ok"
        finally:
            again.shutdown()

    def test_a_changed_resource_is_never_appended_to_the_old_partial(self, flow, server, tmp_path):
        _, _, job_id = _interrupt(flow, server)
        partial = [p for p in (flow.root / ".work").rglob("*.part")]
        assert len(partial) == 1
        kept = partial[0].read_bytes()
        flow.shutdown()
        server.state.reset(Scenario(kind="progressive", etag=True, media_revision=1, pad_bytes=PAD))
        again = Flow(server, tmp_path, "fs")
        try:
            again.data("executor_start")
            job = again.wait_job(job_id, "waiting")
            assert job["wait_reason"] == "source_decision", job
            assert again.data("library_list")["items"] == []
            assert partial[0].read_bytes()[: len(kept)] == kept
            assert not any(e["method"] == "GET" and e["range"] for e in _requests(server))
        finally:
            again.shutdown()

    def test_a_paused_job_keeps_its_checkpoint_across_a_restart(self, flow, server, tmp_path):
        server.state.reset(_held_scenario())
        _, _, job_id = flow.enqueue()
        flow.data("executor_start")
        wait_until(lambda: _held(server, flow.root))
        job = flow.job(job_id)
        flow.data("queue_pause", job_id=job_id, expected_revision=job["revision"])
        _release_after_the_monitor(server)
        paused = flow.wait_job(job_id, "paused")
        part = next((flow.root / ".work").rglob("*.part"))
        assert part.stat().st_size >= paused["bytes_done"] > 0
        flow.shutdown()
        server.state.reset(Scenario(kind="progressive", etag=True, pad_bytes=PAD))
        again = Flow(server, tmp_path, "fs")
        try:
            again.data("executor_start")
            job = again.job(job_id)
            assert job["state"] == "paused"
            again.data("queue_resume", job_id=job_id, expected_revision=job["revision"])
            again.wait_job(job_id, "completed")
            assert any(e["method"] == "GET" and e["range"] not in (None, "bytes=0-") for e in _requests(server))
        finally:
            again.shutdown()

    def test_cancel_during_a_transfer_removes_the_partial_once_the_io_ended(self, flow, server):
        server.state.reset(_held_scenario())
        _, _, job_id = flow.enqueue()
        flow.data("executor_start")
        wait_until(lambda: _held(server, flow.root))
        assert list((flow.root / ".work").rglob("*.part"))
        flow.data("queue_cancel", job_id=job_id, expected_revision=flow.job(job_id)["revision"])
        _release_after_the_monitor(server)
        flow.wait_job(job_id, "cancelled")
        wait_until(lambda: not list((flow.root / ".work").rglob("*/*")))
        assert flow.data("library_list")["items"] == []


class TestNetworkAndPreferences:
    def test_losing_the_network_holds_the_job_and_its_return_completes_it(self, flow, server):
        server.state.reset(_held_scenario())
        _, _, job_id = flow.enqueue()
        flow.data("executor_start")
        wait_until(lambda: _held(server, flow.root))
        flow.data("network_set", kind="offline")
        _release_after_the_monitor(server)
        job = flow.wait_job(job_id, "waiting")
        assert (job["wait_reason"], job["retry_count"]) == ("network", 0), job
        flow.data("network_set", kind="wifi")
        flow.wait_job(job_id, "completed")

    def test_switching_to_wifi_only_stops_a_cellular_transfer(self, flow, server):
        prefs = flow.data("preferences_get")
        prefs = flow.data("preferences_update", changes={"download_network": "any"}, expected_revision=prefs["revision"])
        flow.data("network_set", kind="cellular")
        server.state.reset(_held_scenario())
        _, _, job_id = flow.enqueue()
        flow.data("executor_start")
        wait_until(lambda: _held(server, flow.root))
        flow.data("preferences_update", changes={"download_network": "wifi_only"}, expected_revision=prefs["revision"])
        _release_after_the_monitor(server)
        assert flow.wait_job(job_id, "waiting")["wait_reason"] == "network"


class TestOwnershipAndRoot:
    def test_below_api_30_the_executor_refuses_with_a_readable_code_and_keeps_no_ownership(self, server, tmp_path):
        flow = Flow(server, tmp_path, "fs", api_level=29)
        try:
            for _ in range(2):
                error = flow.error("executor_start")
                assert (error["code"], error["kind"]) == ("storage.no_atomic_publish", "storage")
            assert flow.data("executor_status")["state"] == "stopped"
        finally:
            flow.shutdown()

    def test_a_second_bridge_on_the_same_database_is_busy(self, flow, server, tmp_path):
        assert flow.data("executor_start")["already_running"] is False
        assert flow.data("executor_start")["already_running"] is True
        other = Flow(server, tmp_path, "fs")
        try:
            assert other.error("executor_start")["code"] == "executor.busy"
        finally:
            other.shutdown()

    def test_a_foreign_file_takes_no_name_and_is_reported_on_every_start_but_never_deleted(self, flow, server, tmp_path):
        series, _ = flow.listing()
        foreign = flow.root / f"{series['title']} [{series['id']}]" / f"{series['title']} - ep1.mp4"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"theirs")
        stray = flow.root / "notes" / "keep.txt"
        stray.parent.mkdir()
        stray.write_bytes(b"mine")
        _download(flow, server)
        (item,) = flow.data("library_list")["items"]
        assert foreign.read_bytes() == b"theirs"
        flow.shutdown()
        for _ in range(3):
            again = Flow(server, tmp_path, "fs")
            try:
                # the counted orphans are the foreign file and the stray note; the keys travel as orphan_found events
                assert again.data("executor_start")["recovery"]["orphans"] == 2
                assert len(again.data("library_list")["items"]) == 1
            finally:
                again.shutdown()
        assert foreign.read_bytes() == b"theirs" and stray.read_bytes() == b"mine"
        assert item["item_id"]


class TestDeletion:
    def test_deleting_an_item_removes_its_file_and_keeps_the_watch_state(self, flow, server):
        series, episode, _ = _download(flow, server)
        local = flow.local_target(series, episode)
        path = Path(local["target"]["value"])
        before = flow.data("watch_get", series_id=series["id"], numbers=[episode["number"]])
        (item,) = flow.data("library_list")["items"]
        (result,) = flow.data("library_delete", item_ids=[item["item_id"]])["results"]
        assert result["result"] == "ok", result
        assert not path.exists()
        assert flow.data("library_list")["items"] == []
        assert flow.data("watch_get", series_id=series["id"], numbers=[episode["number"]]) == before

    def test_a_failed_delete_stays_retryable_and_finishes_after_a_restart(self, flow, server, tmp_path):
        series, episode, _ = _download(flow, server)
        path = Path(flow.local_target(series, episode)["target"]["value"])
        (item,) = flow.data("library_list")["items"]
        locked = _lock(path)
        try:
            (result,) = flow.data("library_delete", item_ids=[item["item_id"]])["results"]
            assert (result["result"], result["code"]) == ("failed", "storage.access_denied"), result
            assert flow.data("library_item", item_id=item["item_id"])["item"]["status"] == "deleting"
        finally:
            _unlock(locked)
        flow.shutdown()
        again = Flow(server, tmp_path, "fs")
        try:
            report = again.data("executor_start")["recovery"]
            assert report["counts"]["deletes_finished"] == 1, report
            assert not path.exists() and again.data("library_list")["items"] == []
        finally:
            again.shutdown()


class TestRestoredDatabase:
    def test_a_database_restored_without_its_library_marks_items_missing_and_keeps_history(self, flow, server, tmp_path):
        series, episode, _ = _download(flow, server)
        session = flow.data("playback_begin", series=series, episode=episode)["playback_session"]
        flow.data("playback_position", playback_session=session, seq=1, position_ms=42_000, duration_ms=100_000)
        watch = flow.data("watch_get", series_id=series["id"], numbers=[episode["number"]])
        prefs = flow.data("preferences_get")
        jobs = flow.data("queue_snapshot")["jobs"]
        flow.shutdown()
        shutil.rmtree(tmp_path / "library")
        again = Flow(server, tmp_path, "fs")
        try:
            again.data("executor_start")
            (item,) = again.data("library_list")["items"]
            assert item["status"] == "missing"
            assert again.data("watch_get", series_id=series["id"], numbers=[episode["number"]]) == watch
            assert again.data("preferences_get") == prefs
            assert again.data("queue_snapshot")["jobs"] == jobs
            assert again.local_target(series, episode)["mode"] == "online"
        finally:
            again.shutdown()

    def test_a_database_created_without_a_file_store_opens_with_one(self, server, tmp_path):
        server.state.reset(Scenario(kind="progressive"))
        legacy = Flow(server, tmp_path)
        series, episode = legacy.listing()
        session = legacy.data("playback_begin", series=series, episode=episode)["playback_session"]
        legacy.data("playback_position", playback_session=session, seq=1, position_ms=5_000, duration_ms=100_000)
        watch = legacy.data("watch_get", series_id=series["id"], numbers=[episode["number"]])
        assert legacy.error("executor_start")["code"] == "storage.not_configured"
        legacy.shutdown()
        current = Flow(server, tmp_path, "fs")
        try:
            report = current.data("executor_start")["recovery"]
            assert report["orphans"] == 0 and not any(report["counts"].values()), report
            assert current.data("watch_get", series_id=series["id"], numbers=[episode["number"]]) == watch
        finally:
            current.shutdown()


def _lock(path: Path):
    """Makes removing ``path`` fail with an access error, the way a read-only directory does on Android."""
    if sys.platform == "win32":
        os.chmod(path, stat.S_IREAD)
        return path
    os.chmod(path.parent, 0o500)
    return path.parent


def _unlock(path: Path) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD if sys.platform == "win32" else 0o700)
