"""The production Android file store: the API gate, no-replace publishing and the work-area tidy-up at executor start."""

import errno
import json
import os
import sys

import android_storage
import pytest
from android_storage import MIN_ATOMIC_RENAME_API, AppStorageStore

from alt_ani_bridge import Bridge
from alt_ani_core.errors import StorageError
from alt_ani_core.storage import api
from alt_ani_core.storage.api import PublishName

NAME = PublishName(series_id="12", series_title="Frieren", episode_label="1", extension="mp4")
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="renameat2 is a Linux system call")
posix_only = pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")


@pytest.fixture
def store(tmp_path):
    s = AppStorageStore(tmp_path / "library", api_level=MIN_ATOMIC_RENAME_API)
    s.check_root()
    return s


def _local(tmp_path, data: bytes = b"video"):
    path = tmp_path / "work.bin"
    path.write_bytes(data)
    return path


@pytest.mark.unit
class TestApiGate:
    def test_below_api_30_the_store_refuses_without_the_system_call(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(android_storage, "rename_noreplace", lambda *a: calls.append(a))
        store = AppStorageStore(tmp_path / "library", api_level=MIN_ATOMIC_RENAME_API - 1)
        for action in (store.check_root, lambda: store.publish(_local(tmp_path), store.allocate(NAME))):
            with pytest.raises(StorageError) as exc:
                action()
            assert (exc.value.code, exc.value.params["reason"]) == (api.NO_ATOMIC_PUBLISH, "api_level")
        assert calls == []
        assert not (tmp_path / "library").exists()

    def test_on_android_a_missing_api_level_is_a_refusal_not_a_guess(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "android")
        store = AppStorageStore(tmp_path / "library")
        with pytest.raises(StorageError) as exc:
            store.check_root()
        assert (exc.value.code, exc.value.params["reason"]) == (api.NO_ATOMIC_PUBLISH, "api_level_unknown")

    def test_from_api_30_the_root_is_accepted(self, store):
        assert (store.root / ".work").is_dir()

    def test_register_returns_the_platform_storage_argument(self, tmp_path):
        bridge = Bridge()
        reply = json.loads(android_storage.register(bridge, str(tmp_path / "library"), api_level=31))
        assert reply["kind"] == "platform" and reply["handle"]


@pytest.mark.unit
@linux_only
class TestRenamePublish:
    def test_rename_never_replaces_an_existing_file(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        with pytest.raises(FileExistsError):
            android_storage.rename_noreplace(src, dst)
        assert dst.read_bytes() == b"old"
        dst.unlink()
        android_storage.rename_noreplace(src, dst)
        assert dst.read_bytes() == b"new" and not src.exists()

    def test_a_dangling_symlink_at_the_target_is_never_replaced(self, store, tmp_path):
        target = store.allocate(NAME)
        path = store.root / target.key
        path.parent.mkdir(parents=True)
        path.symlink_to(store.root / "nowhere.mp4")
        with pytest.raises(StorageError) as exc:
            store.publish(_local(tmp_path), target)
        assert exc.value.code == api.TARGET_EXISTS
        assert path.is_symlink() and (tmp_path / "work.bin").exists()

    def test_a_failing_probe_is_no_atomic_publish_and_leaves_nothing(self, tmp_path, monkeypatch):
        def refused(*_):
            raise OSError(errno.ENOSYS, "no")

        monkeypatch.setattr(android_storage, "rename_noreplace", refused)
        store = AppStorageStore(tmp_path / "library", api_level=33)
        with pytest.raises(StorageError) as exc:
            store.check_root()
        assert (exc.value.code, exc.value.params["errno"]) == (api.NO_ATOMIC_PUBLISH, errno.ENOSYS)
        assert os.listdir(tmp_path / "library" / ".work") == []

    @pytest.mark.parametrize(
        ("err", "code"),
        [(errno.ENOSPC, api.NO_SPACE), (errno.EACCES, api.ACCESS_DENIED), (errno.ENOSYS, api.FAILED)],
    )
    def test_rename_errors_become_coded_storage_errors(self, store, tmp_path, monkeypatch, err, code):
        def failing(*_):
            raise OSError(err, "/secret/path")

        monkeypatch.setattr(android_storage, "rename_noreplace", failing)
        with pytest.raises(StorageError) as exc:
            store.publish(_local(tmp_path), store.allocate(NAME))
        assert exc.value.code == code
        assert "/secret/path" not in str(exc.value)
        assert (tmp_path / "work.bin").exists()


@pytest.mark.unit
class TestWorkTidyUp:
    def test_empty_job_directories_and_stale_probes_go(self, store):
        work = store.root / ".work"
        (work / "job-1").mkdir()
        (work / f".probe-{'a' * 32}").write_bytes(b"")
        (work / f".probe-{'b' * 32}-moved").write_bytes(b"")
        store.check_root()
        assert os.listdir(work) == []

    def test_a_job_directory_with_a_partial_is_kept_whole(self, store):
        partial = store.work_path(store.partial_locator("job-2", "p1"))
        (partial / "media.mp4.part").write_bytes(b"x" * 10)
        empty_partial = store.work_path(store.partial_locator("job-3", "p1"))
        store.check_root()
        assert (partial / "media.mp4.part").read_bytes() == b"x" * 10
        assert empty_partial.is_dir()

    def test_unknown_names_are_never_touched(self, store):
        work = store.root / ".work"
        (work / "not a job").mkdir()
        (work / "notes.txt").write_bytes(b"mine")
        (work / ".probe-short").write_bytes(b"")
        (work / f".probe-{'c' * 32}").mkdir()
        store.check_root()
        assert sorted(os.listdir(work)) == sorted(["not a job", "notes.txt", ".probe-short", f".probe-{'c' * 32}"])

    def test_published_and_series_directories_are_outside_the_tidy_up(self, store, tmp_path):
        (store.root / "Empty [1]").mkdir()
        target = store.allocate(NAME)
        store.publish(_local(tmp_path), target)
        store.check_root()
        assert (store.root / "Empty [1]").is_dir() and store.stat(target).size == 5

    @posix_only
    def test_a_symlink_to_a_directory_is_never_followed_or_removed(self, store, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (store.root / ".work" / "job-4").symlink_to(outside)
        (store.root / ".work" / f".probe-{'d' * 32}").symlink_to(tmp_path / "victim")
        (tmp_path / "victim").write_bytes(b"keep")
        store.check_root()
        assert (store.root / ".work" / "job-4").is_symlink() and outside.is_dir()
        assert (tmp_path / "victim").read_bytes() == b"keep"

    def test_a_failing_removal_never_fails_the_root_check(self, store, monkeypatch):
        (store.root / ".work" / "job-5").mkdir()

        def busy(_path):
            raise OSError(errno.EBUSY, "busy")

        monkeypatch.setattr(android_storage.os, "rmdir", busy)
        store.check_root()
        assert (store.root / ".work" / "job-5").is_dir()

    def test_tidying_twice_changes_nothing_more(self, store):
        (store.root / ".work" / "job-6").mkdir()
        keep = store.work_path(store.partial_locator("job-7", "p1"))
        store.check_root()
        first = sorted(p.relative_to(store.root) for p in store.root.rglob("*"))
        store.check_root()
        assert sorted(p.relative_to(store.root) for p in store.root.rglob("*")) == first
        assert keep.is_dir()


@pytest.mark.unit
@posix_only
class TestSymlinks:
    def test_a_symlink_out_of_the_root_is_refused_and_its_target_kept(self, store, tmp_path):
        victim = tmp_path / "victim.mp4"
        victim.write_bytes(b"keep")
        (store.root / "Frieren [12]").mkdir()
        (store.root / "Frieren [12]" / "Frieren - ep1.mp4").symlink_to(victim)
        locator = store.allocate(PublishName("12", "Frieren", "2", "mp4"))
        link = type(locator)("fs", "Frieren [12]/Frieren - ep1.mp4")
        for action in (store.stat, store.remove, store.playback_target):
            with pytest.raises(StorageError) as exc:
                action(link)
            assert exc.value.code == api.OUTSIDE_ROOT
        assert victim.read_bytes() == b"keep"

    def test_removing_a_symlink_inside_the_root_leaves_its_target(self, store, tmp_path):
        target = store.allocate(NAME)
        store.publish(_local(tmp_path), target)
        (store.root / "alias.mp4").symlink_to(store.root / target.key)
        assert store.remove(type(target)("fs", "alias.mp4")) is True
        assert store.stat(target).size == 5
