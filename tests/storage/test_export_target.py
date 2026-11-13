"""The export protocol: nothing at the destination before ``commit``; a cancelled copy leaves no file and no temporary."""

import io
import threading

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.errors import OperationCancelled, StorageError
from alt_ani_core.storage import fs


class GatedSource(io.BytesIO):
    """Reads park at the second chunk until released, so a test can cancel mid-copy deterministically."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.parked = threading.Event()
        self.release = threading.Event()
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        if self.reads == 2:
            self.parked.set()
            assert self.release.wait(5)
        return super().read(4)


@pytest.mark.unit
class TestDirectoryExportTarget:
    def test_commit_publishes_under_a_free_name(self, tmp_path):
        target = fs.DirectoryExportTarget(tmp_path)
        (tmp_path / "a.mp4").write_bytes(b"old")
        writer = target.create("a.mp4", on_conflict="rename")
        writer.write(b"new")
        assert writer.commit() == "a (2).mp4"
        assert (tmp_path / "a.mp4").read_bytes() == b"old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a (2).mp4", "a.mp4"]

    def test_fail_on_conflict_leaves_no_temporary(self, tmp_path):
        (tmp_path / "a.mp4").write_bytes(b"old")
        writer = fs.DirectoryExportTarget(tmp_path).create("a.mp4", on_conflict="fail")
        writer.write(b"new")
        with pytest.raises(StorageError) as err:
            writer.commit()
        assert err.value.code == "storage.target_exists"
        assert [p.name for p in tmp_path.iterdir()] == ["a.mp4"]

    def test_cancel_mid_copy_leaves_nothing(self, tmp_path):
        source, token = GatedSource(b"0123456789abcdef"), CancelToken()
        writer = fs.DirectoryExportTarget(tmp_path / "out").create("a.mp4", on_conflict="fail")
        outcome: list = []

        def run():
            try:
                outcome.append(fs.copy_to(source, writer, token))
            except OperationCancelled:
                outcome.append("cancelled")

        thread = threading.Thread(target=run)
        thread.start()
        assert source.parked.wait(5)
        token.cancel()
        source.release.set()
        thread.join(5)
        assert outcome == ["cancelled"]
        assert list((tmp_path / "out").iterdir()) == []

    def test_unknown_conflict_policy(self, tmp_path):
        with pytest.raises(ValueError):
            fs.DirectoryExportTarget(tmp_path).create("a.mp4", on_conflict="replace")

    def test_playback_target_is_a_local_file(self, tmp_path):
        from alt_ani_core.state import StorageLocator

        store = fs.FsFileStore(tmp_path)
        target = store.playback_target(StorageLocator("fs", "Fate/ep1.mp4"))
        assert target.kind == "file"
        assert target.value == str(tmp_path / "Fate" / "ep1.mp4")
