import errno
import os
from pathlib import Path

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.errors import OperationCancelled, StorageError
from alt_ani_core.state import StorageLocator
from alt_ani_core.storage import api, fs, naming
from alt_ani_core.storage.api import PublishName

NAME = PublishName(series_id="12", series_title="Fate/Zero: Ep?", episode_label="1", extension="mp4")


@pytest.fixture
def store(tmp_path):
    s = fs.FsFileStore(tmp_path / "library")
    s.check_root()
    return s


def _local(tmp_path, data: bytes = b"video") -> Path:
    path = tmp_path / "work.bin"
    path.write_bytes(data)
    return path


@pytest.mark.unit
class TestNaming:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Fate/Zero", "FateZero"),
            ("  a   b  ", "a b"),
            ("../..", "x"),
            ("CON", "x"),
            ("nul.txt", "nultxt"),
            ("trailing. ", "trailing"),
            ("", "x"),
            ("Ｆａｔｅ", "Ｆａｔｅ"),
        ],
    )
    def test_component_is_safe(self, text, expected):
        assert naming.safe_component(text, "x") == expected

    def test_long_titles_stay_within_the_component_limit(self):
        key = naming.publish_key("1", "ą" * 300, "12", "mp4")
        assert all(len(part.encode()) <= naming.MAX_COMPONENT_BYTES for part in key.split("/"))

    def test_collisions_get_a_number(self):
        assert naming.publish_key("1", "Fate", "2", "mp4", 3) == "Fate [1]/Fate - ep2 (3).mp4"

    def test_extension_is_reduced_to_ascii_alphanumerics(self):
        assert naming.safe_extension("M P4/../") == "mp4"
        assert naming.safe_extension("") == "bin"


@pytest.mark.unit
class TestBoundaries:
    @pytest.mark.parametrize("key", ["../escape.mp4", "/etc/passwd", "C:/x.mp4", "a/../../b", "a\\..\\b", "a//b", "./a"])
    def test_keys_outside_the_root_are_refused(self, store, key):
        with pytest.raises(StorageError) as exc:
            store.stat(StorageLocator("fs", key))
        assert exc.value.code == api.OUTSIDE_ROOT

    def test_other_backends_are_refused(self, store):
        with pytest.raises(StorageError):
            store.stat(StorageLocator("content", "x"))

    @pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
    def test_symlink_out_of_the_root_is_refused(self, store, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (store.root / "link").symlink_to(outside)
        with pytest.raises(StorageError):
            store.stat(StorageLocator("fs", "link/x.mp4"))

    def test_work_path_only_inside_the_work_area(self, store):
        with pytest.raises(StorageError):
            store.work_path(StorageLocator("fs", "Fate [1]/"))
        assert store.work_path(store.partial_locator("job", "p1")).is_dir()

    def test_partial_ids_must_be_plain(self, store):
        with pytest.raises(StorageError):
            store.partial_locator("../x", "p")


@pytest.mark.unit
class TestPublish:
    def test_publishes_without_leaving_the_work_file(self, store, tmp_path):
        target = store.allocate(NAME)
        store.publish(_local(tmp_path), target)
        assert store.stat(target).size == 5
        assert not (tmp_path / "work.bin").exists()

    def test_never_replaces_an_existing_file(self, store, tmp_path):
        target = store.allocate(NAME)
        path = store.root / target.key
        path.parent.mkdir(parents=True)
        path.write_bytes(b"theirs")
        with pytest.raises(StorageError) as exc:
            store.publish(_local(tmp_path), target)
        assert exc.value.code == api.TARGET_EXISTS
        assert path.read_bytes() == b"theirs"
        assert (tmp_path / "work.bin").exists()

    def test_allocate_skips_names_taken_on_disk(self, store):
        first = store.allocate(NAME)
        (store.root / first.key).parent.mkdir(parents=True)
        (store.root / first.key).write_bytes(b"x")
        assert store.allocate(NAME).key.endswith(" (2).mp4")

    @pytest.mark.skipif(os.name != "posix", reason="hard links are the POSIX publish path")
    def test_file_system_without_hard_links_is_refused(self, store, monkeypatch):
        def no_links(*_):
            raise OSError(errno.EPERM, "no")

        monkeypatch.setattr(os, "link", no_links)
        with pytest.raises(StorageError) as exc:
            store.check_root()
        assert exc.value.code == api.NO_ATOMIC_PUBLISH

    @pytest.mark.skipif(os.name != "posix", reason="POSIX publish path")
    def test_directory_is_synced_between_link_and_unlink(self, store, tmp_path, monkeypatch):
        events: list[str] = []
        real_link, real_unlink, real_fsync_dir = os.link, os.unlink, fs.fsync_dir
        monkeypatch.setattr(os, "link", lambda a, b: (events.append("link"), real_link(a, b))[1])
        monkeypatch.setattr(os, "unlink", lambda p: (events.append("unlink"), real_unlink(p))[1])
        monkeypatch.setattr(fs, "fsync_dir", lambda p: (events.append("fsync_dir"), real_fsync_dir(p))[1])
        store.publish(_local(tmp_path), store.allocate(NAME))
        assert events == ["link", "fsync_dir", "unlink"]

    @pytest.mark.parametrize(
        ("err", "code"),
        [(errno.ENOSPC, api.NO_SPACE), (errno.EACCES, api.ACCESS_DENIED), (errno.EROFS, api.ACCESS_DENIED)],
    )
    def test_os_errors_become_coded_storage_errors(self, store, tmp_path, monkeypatch, err, code):
        def failing(*_):
            raise OSError(err, "/secret/path")

        monkeypatch.setattr(fs, "fsync_file", failing)
        with pytest.raises(StorageError) as exc:
            store.publish(_local(tmp_path), store.allocate(NAME))
        assert exc.value.code == code
        assert "/secret/path" not in str(exc.value)


@pytest.mark.unit
class TestRemoveAndScan:
    def test_remove_reports_whether_something_was_there(self, store, tmp_path):
        target = store.allocate(NAME)
        store.publish(_local(tmp_path), target)
        assert store.remove(target) is True
        assert store.remove(target) is False

    def test_work_directory_is_removed_whole(self, store):
        loc = store.partial_locator("job", "p1")
        (store.work_path(loc) / "media.mp4.part").write_bytes(b"x")
        assert store.remove(loc) is True
        assert not (store.root / ".work" / "job" / "p1").exists()

    def test_a_published_directory_is_never_removed_whole(self, store):
        (store.root / "Fate [1]").mkdir()
        with pytest.raises(StorageError):
            store.remove(StorageLocator("fs", "Fate [1]/"))

    def test_scan_lists_files_and_partials(self, store, tmp_path):
        target = store.allocate(NAME)
        store.publish(_local(tmp_path), target)
        partial = store.partial_locator("job", "p1")
        store.work_path(partial)
        assert set(store.scan()) == {target, partial}

    def test_digest_honours_cancellation(self, store, tmp_path):
        target = store.allocate(NAME)
        store.publish(_local(tmp_path, b"x" * (3 * 1024 * 1024)), target)
        token = CancelToken()
        token.cancel()
        with pytest.raises(OperationCancelled):
            store.digest(target, token)


@pytest.mark.unit
class TestExport:
    def test_copy_is_independent_of_the_original(self, store, tmp_path):
        target = store.allocate(NAME)
        store.publish(_local(tmp_path, b"original"), target)
        with store.open_read(target) as fh:
            copy = fs.export_copy(fh, tmp_path / "out", "Fate - ep1.mp4")
        store.remove(target)
        assert copy.read_bytes() == b"original"

    def test_existing_destination_is_kept_and_a_number_added(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "Fate - ep1.mp4").write_bytes(b"mine")
        with (tmp_path / "src").open("wb") as fh:
            fh.write(b"new")
        with (tmp_path / "src").open("rb") as fh:
            copy = fs.export_copy(fh, out, "Fate - ep1.mp4")
        assert copy.name == "Fate - ep1 (2).mp4"
        assert (out / "Fate - ep1.mp4").read_bytes() == b"mine"

    def test_conflict_can_be_refused(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "a.mp4").write_bytes(b"mine")
        (tmp_path / "src").write_bytes(b"new")
        with (tmp_path / "src").open("rb") as fh, pytest.raises(StorageError):
            fs.export_copy(fh, out, "a.mp4", rename_on_conflict=False)
        assert sorted(p.name for p in out.iterdir()) == ["a.mp4"]

    def test_interrupted_export_leaves_nothing(self, tmp_path):
        (tmp_path / "src").write_bytes(b"x" * (3 * 1024 * 1024))
        token = CancelToken()
        token.cancel()
        with (tmp_path / "src").open("rb") as fh, pytest.raises(OperationCancelled):
            fs.export_copy(fh, tmp_path / "out", "a.mp4", cancel=token)
        assert list((tmp_path / "out").iterdir()) == []
