"""Library through the contract: one result per item, honest cancellation, history untouched."""

import json
import threading

from .conftest import SERIES, ep


class MemoryTarget:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.published: dict[str, bytes] = {}
        self.gate = gate
        self.parked = threading.Event()

    def create(self, name: str, *, on_conflict: str):
        target = self

        class Writer:
            data = b""

            def write(self, chunk: bytes) -> None:
                if target.gate is not None:
                    target.parked.set()
                    assert target.gate.wait(5)
                self.data += chunk

            def commit(self) -> str:
                target.published[name] = self.data
                return name

            def abort(self) -> None:
                pass

        return Writer()


def _register(client, obj) -> str:
    return json.loads(client.bridge.register_platform_object(obj))["data"]["handle"]


class TestBatches:
    def test_list_item_and_watch_view(self, v2):
        (item,) = v2.download(1)
        listed = v2.data("library_list", series_id="7")["items"]
        assert [i["item_id"] for i in listed] == [item["item_id"]]
        assert "locator" not in json.dumps(listed) and listed[0]["file_name"].endswith(".mp4")
        detail = v2.data("library_item", item_id=item["item_id"])
        assert detail["watch"]["number"] == 1

    def test_delete_keeps_watch_history(self, v2):
        (item,) = v2.download(1)
        session = v2.data("playback_begin", series=SERIES, episode=ep(1))["playback_session"]
        v2.data("playback_position", playback_session=session, seq=1, position_ms=1234)
        result = v2.data("library_delete", item_ids=[item["item_id"], "missing"])
        assert [(r["item_id"], r["result"], r["code"]) for r in result["results"]] == [
            (item["item_id"], "ok", None),
            ("missing", "failed", "state.unknown_item"),
        ]
        assert "interrupted" not in result
        assert v2.data("watch_get", series_id="7", numbers=[1])["episodes"][0]["position_ms"] == 1234

    def test_reserved_locator_is_refused(self, v2):
        (item,) = v2.download(1)
        (job,) = v2.enqueue((2,))
        (runtime,) = v2.bridge._runtimes.values()
        with runtime.state.database.write() as conn:
            key = conn.execute("SELECT storage_key FROM offline_item WHERE id = ?", (item["item_id"],)).fetchone()[0]
            conn.execute("UPDATE download_job SET target_backend = 'fs', target_key = ? WHERE id = ?", (key, job["job_id"]))
        (result,) = v2.data("library_delete", item_ids=[item["item_id"]])["results"]
        assert (result["result"], result["code"]) == ("failed", "state.locator_reserved")

    def test_verify(self, v2):
        (item,) = v2.download(1)
        (result,) = v2.data("library_verify", deep=True)["results"]
        assert (result["item_id"], result["result"]) == (item["item_id"], "ok")


class TestExport:
    def test_directory_fail_then_rename(self, v2, tmp_path):
        (item,) = v2.download(1)
        target = {"kind": "directory", "path": str(tmp_path / "out")}
        (first,) = v2.data("library_export", item_ids=[item["item_id"]], target=target)["results"]
        (repeat,) = v2.data("library_export", item_ids=[item["item_id"]], target=target)["results"]
        assert first["result"] == "ok" and repeat["code"] == "storage.target_exists"
        (renamed,) = v2.data("library_export", item_ids=[item["item_id"]], target=target, on_conflict="rename")["results"]
        assert renamed["file_name"] != first["file_name"]
        assert str(tmp_path) not in json.dumps([first, renamed])

    def test_platform_target(self, v2):
        (item,) = v2.download(1)
        target = MemoryTarget()
        handle = _register(v2, target)
        (result,) = v2.data("library_export", item_ids=[item["item_id"]], target={"kind": "platform", "handle": handle})[
            "results"
        ]
        assert target.published[result["file_name"]] == b"video-bytes"
        assert json.loads(v2.bridge.release_platform_object(handle))["ok"]

    def test_cancel_before_publish_leaves_nothing(self, v2):
        items = v2.download(1, 2)
        gate = threading.Event()
        target = MemoryTarget(gate)
        handle = _register(v2, target)
        replies: list = []
        thread = threading.Thread(
            target=lambda: replies.append(
                v2.call(
                    "library_export",
                    item_ids=[i["item_id"] for i in items],
                    target={"kind": "platform", "handle": handle},
                    op_id="export-1",
                )
            )
        )
        thread.start()
        assert target.parked.wait(5)
        # in use while the export runs
        assert json.loads(v2.bridge.release_platform_object(handle))["error"]["code"] == "platform.in_use"
        assert v2.bridge.cancel("export-1")
        gate.set()
        thread.join(5)
        data = replies[0]["data"]
        assert data["interrupted"] == "cancelled"
        assert data["results"] == [{"item_id": items[0]["item_id"], "result": "failed", "code": "cancelled"}]
        assert data["not_attempted"] == [items[1]["item_id"]]
        assert target.published == {}

    def test_cancel_after_publish_reports_the_file(self, v2, tmp_path):
        (item,) = v2.download(1)

        class CancelAfterCommit(MemoryTarget):
            def create(self, name, *, on_conflict):
                writer = super().create(name, on_conflict=on_conflict)
                commit = writer.commit

                def committed():
                    final = commit()
                    v2.bridge.cancel("export-2")
                    return final

                writer.commit = committed
                return writer

        target = CancelAfterCommit()
        handle = _register(v2, target)
        reply = v2.data(
            "library_export", item_ids=[item["item_id"]], target={"kind": "platform", "handle": handle}, op_id="export-2"
        )
        assert reply["results"][0]["result"] == "ok" and "interrupted" not in reply
        assert target.published


class TestDeleteRetry:
    def test_interrupted_delete_is_finished_later(self, v2):
        from unittest.mock import patch

        from alt_ani_core.storage.fs import FsFileStore

        items = v2.download(1, 2)
        real_digest = FsFileStore.digest

        def cancelling_digest(store, locator, cancel=None):
            v2.bridge.cancel("delete-1")
            return real_digest(store, locator, cancel)

        with patch.object(FsFileStore, "digest", cancelling_digest):
            data = v2.data("library_delete", item_ids=[i["item_id"] for i in items], op_id="delete-1")
        assert data["interrupted"] == "cancelled"
        assert data["results"][0]["code"] == "cancelled" and data["not_attempted"] == [items[1]["item_id"]]
        assert v2.data("library_item", item_id=items[0]["item_id"])["item"]["status"] == "deleting"
        retried = v2.data("library_retry_deletes")
        assert [(r["item_id"], r["result"]) for r in retried["results"]] == [(items[0]["item_id"], "ok")]
