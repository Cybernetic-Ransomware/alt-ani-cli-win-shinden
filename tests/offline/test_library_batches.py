"""Per-item batches with honest cancellation: not touched items are listed, a published export is reported."""

import hashlib
import io
import threading

import pytest
from history.state_support import owner, ref, release_owners, rows

from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.history.local import open_local_state
from alt_ani_core.services.offline.library import LibraryService, ResultKind
from alt_ani_core.services.offline.sources import EngineSources
from alt_ani_core.state import DownloadCriteria, ItemStatus, NetworkKind, OfflineItemSpec, StorageLocator
from alt_ani_core.storage.fs import FsFileStore


@pytest.fixture(autouse=True)
def _owners():
    yield
    release_owners()


def make_library(tmp_path, count: int = 3, body: bytes = b"0123456789" * 10):
    state = open_local_state(tmp_path / "history.db")
    store = FsFileStore(tmp_path / "library")
    store.check_root()
    items = []
    for number in range(1, count + 1):
        name = f"ep{number}.mp4"
        (tmp_path / "library" / name).write_bytes(body)
        (job,) = state.queue.enqueue(ref(), rows(number), DownloadCriteria()).added
        claim = state.queue.claim_next(NetworkKind.WIFI, owner(state))
        assert claim is not None and state.queue.begin_finalize(claim.attempt_id)
        spec = OfflineItemSpec(
            StorageLocator("fs", name),
            size_bytes=len(body),
            audio="jp",
            subs="pl",
            height=720,
            sha256=hashlib.sha256(body).hexdigest(),
        )
        items.append(state.queue.complete(claim.attempt_id, spec))
    return state, store, LibraryService(state.library, store), items


class CancellingStore(FsFileStore):
    """Cancels the token when the given item's file is read, as a user tapping cancel mid-operation would."""

    def __init__(self, root, token: CancelToken, key: str) -> None:
        super().__init__(root)
        self.token, self.key = token, key

    def digest(self, locator, cancel=None):
        if locator.key == self.key:
            self.token.cancel()
        return super().digest(locator, cancel or self.token)

    def open_read(self, locator):
        if locator.key == self.key:
            self.token.cancel()
        return super().open_read(locator)


class MemoryTarget:
    """A platform destination with no path; ``gate`` parks the first write."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.published: dict[str, bytes] = {}
        self.aborted = 0
        self.gate = gate
        self.parked = threading.Event()

    def create(self, name: str, *, on_conflict: str):
        target = self

        class Writer:
            def __init__(self) -> None:
                self.buf = io.BytesIO()

            def write(self, chunk: bytes) -> None:
                if target.gate is not None:
                    target.parked.set()
                    assert target.gate.wait(5)
                self.buf.write(chunk)

            def commit(self) -> str:
                final = name if name not in target.published or on_conflict == "fail" else f"copy-{name}"
                target.published[final] = self.buf.getvalue()
                return final

            def abort(self) -> None:
                target.aborted += 1

        return Writer()


@pytest.mark.unit
class TestCancelledBatches:
    def test_delete_stops_between_items(self, tmp_path):
        state, _store, _service, items = make_library(tmp_path)
        token = CancelToken()
        service = LibraryService(state.library, CancellingStore(tmp_path / "library", token, "ep2.mp4"))
        results = service.delete([i.id for i in items], cancel=token)
        kinds = [(r.item_id, r.kind, r.code) for r in results]
        assert kinds[0] == (items[0].id, ResultKind.OK, None)
        assert kinds[1] == (items[1].id, ResultKind.FAILED, "cancelled")
        assert kinds[2] == (items[2].id, ResultKind.NOT_ATTEMPTED, None)
        # the interrupted item stays deleting for a later retry; the untouched one is still available
        assert state.library.get(items[1].id).status is ItemStatus.DELETING
        assert state.library.get(items[2].id).status is ItemStatus.AVAILABLE
        retried = LibraryService(state.library, FsFileStore(tmp_path / "library")).retry_pending_deletes()
        assert [(r.item_id, r.kind) for r in retried] == [(items[1].id, ResultKind.OK)]

    def test_verify_leaves_the_interrupted_item_unchanged(self, tmp_path):
        state, _store, _service, items = make_library(tmp_path)
        token = CancelToken()
        service = LibraryService(state.library, CancellingStore(tmp_path / "library", token, "ep2.mp4"))
        results = service.verify([i.id for i in items], deep=True, cancel=token)
        assert [r.kind for r in results] == [ResultKind.OK, ResultKind.FAILED, ResultKind.NOT_ATTEMPTED]
        assert results[1].code == "cancelled"
        assert state.library.get(items[1].id).status is ItemStatus.AVAILABLE

    def test_export_cancelled_before_commit_publishes_nothing(self, tmp_path):
        _state, _store, service, items = make_library(tmp_path, count=2)
        token, gate = CancelToken(), threading.Event()
        target = MemoryTarget(gate)
        out: list = []
        thread = threading.Thread(target=lambda: out.append(service.export([i.id for i in items], target, cancel=token)))
        thread.start()
        assert target.parked.wait(5)
        token.cancel()
        gate.set()
        thread.join(5)
        (results,) = out
        assert [(r.kind, r.code) for r in results] == [(ResultKind.FAILED, "cancelled"), (ResultKind.NOT_ATTEMPTED, None)]
        assert target.published == {} and target.aborted == 1

    def test_export_cancelled_after_publish_reports_the_file(self, tmp_path):
        _state, _store, service, items = make_library(tmp_path, count=1)
        token = CancelToken()

        class CancelOnCommit(MemoryTarget):
            def create(self, name, *, on_conflict):
                writer = super().create(name, on_conflict=on_conflict)
                commit = writer.commit

                def committed() -> str:
                    final = commit()
                    token.cancel()
                    return final

                writer.commit = committed
                return writer

        target = CancelOnCommit()
        (result,) = service.export([items[0].id], target, cancel=token)
        assert (result.kind, result.file_name) == (ResultKind.OK, "ep1.mp4")
        assert target.published == {"ep1.mp4": b"0123456789" * 10}

    def test_export_to_a_directory_keeps_the_path(self, tmp_path):
        _state, _store, service, items = make_library(tmp_path, count=1)
        (result,) = service.export([items[0].id], tmp_path / "out", on_conflict="fail")
        assert result.path == tmp_path / "out" / "ep1.mp4" and result.file_name == "ep1.mp4"
        (again,) = service.export([items[0].id], tmp_path / "out", on_conflict="fail")
        assert (again.kind, again.code) == (ResultKind.FAILED, "storage.target_exists")


class RecordingEngine:
    def __init__(self) -> None:
        self.applied: list[AccessConfig] = []

    def set_access(self, access: AccessConfig) -> None:
        self.applied.append(access)


@pytest.mark.unit
class TestEngineSourcesAccess:
    def test_new_base_access_keeps_the_wifi_only_rule(self):
        engine = RecordingEngine()
        sources = EngineSources(engine, AccessConfig(flaresolverr_url="http://solver", use_flaresolverr=True))  # type: ignore[arg-type]
        sources.network_changed(NetworkKind.CELLULAR)
        sources.set_base_access(AccessConfig(flaresolverr_url="http://other", use_flaresolverr=True))
        assert engine.applied[-1] == AccessConfig(flaresolverr_url="http://other", use_flaresolverr=False)
        sources.network_changed(NetworkKind.WIFI)
        assert engine.applied[-1] == AccessConfig(flaresolverr_url="http://other", use_flaresolverr=True)
