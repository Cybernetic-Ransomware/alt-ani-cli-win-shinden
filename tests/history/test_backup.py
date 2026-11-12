import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from contextlib import closing

import pytest

from alt_ani_core.errors import HistoryBackupError
from alt_ani_core.history import database
from alt_ani_core.history.sqlite import SqliteHistory

from .state_support import FIXTURES, scalar


def _v1(path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript((FIXTURES / "history_v1.sql").read_text(encoding="utf-8"))
        conn.commit()


@pytest.fixture
def v1_db(tmp_path):
    path = tmp_path / "history.db"
    _v1(path)
    return path


def _published(path) -> list:
    return sorted(path.parent.glob(database.backup_glob(path, 1)))


def _temps(path) -> list:
    return sorted(path.parent.glob(database.backup_glob(path, 1) + ".tmp*"))


def _assert_valid_v1_copy(backup, original_rows: int = 4) -> None:
    with closing(sqlite3.connect(backup)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == original_rows
        assert conn.execute("SELECT value FROM meta WHERE key = 'legacy_json_sha'").fetchone()[0] == "0123456789abcdef"


def _run_child(db, patch: str) -> subprocess.CompletedProcess:
    """The upgrade in a separate process with a timeout, so a lock stall fails the test instead of hanging it."""
    script = textwrap.dedent(
        f"""
        import os, sys
        from pathlib import Path
        from alt_ani_core.history import database
        from alt_ani_core.history.sqlite import SqliteHistory
        {patch}
        SqliteHistory(Path(sys.argv[1])).watch_entries()
        """
    )
    return subprocess.run([sys.executable, "-c", script, str(db)], timeout=60, check=False, capture_output=True)


@pytest.mark.unit
class TestBackupBeforeUpgrade:
    def test_upgrade_completes_and_publishes_one_verified_copy(self, v1_db):
        result = _run_child(v1_db, "")
        assert result.returncode == 0, result.stderr
        assert scalar(v1_db, "PRAGMA user_version") == 2
        (backup,) = _published(v1_db)
        _assert_valid_v1_copy(backup)
        assert _temps(v1_db) == []

    def test_upgraded_database_is_not_backed_up_again(self, v1_db):
        SqliteHistory(v1_db).watch_entries()
        SqliteHistory(v1_db).record_watch(SqliteHistory(v1_db).watch_entries()[0].series, 9.0)
        assert len(_published(v1_db)) == 1

    def test_copy_has_rows_written_right_before_the_upgrade(self, v1_db):
        with closing(sqlite3.connect(v1_db)) as conn:
            conn.execute("INSERT INTO series (id, slug, title, url) VALUES ('9', 's', 'Late', '')")
            conn.commit()
        SqliteHistory(v1_db).watch_entries()
        _assert_valid_v1_copy(_published(v1_db)[0], original_rows=5)


@pytest.mark.unit
class TestInterruptedBackup:
    def test_killed_after_copying_before_verification(self, v1_db):
        result = _run_child(v1_db, "database._verify_backup = lambda *a: os._exit(3)")
        assert result.returncode == 3
        assert scalar(v1_db, "PRAGMA user_version") == 1
        assert _published(v1_db) == []
        assert _temps(v1_db) != []

        SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 2
        assert _temps(v1_db) == []
        _assert_valid_v1_copy(_published(v1_db)[0])

    def test_killed_while_copying(self, v1_db):
        patch = textwrap.dedent(
            """
            real = database._copy_database
            def copy_and_die(source, target):
                target.write_bytes(b"SQLite format 3\\x00 half written")
                os._exit(3)
            database._copy_database = copy_and_die
            """
        ).replace("\n", "\n        ")
        result = _run_child(v1_db, patch)
        assert result.returncode == 3
        assert scalar(v1_db, "PRAGMA user_version") == 1
        assert _published(v1_db) == []

        SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 2
        assert _temps(v1_db) == []
        _assert_valid_v1_copy(_published(v1_db)[0])

    def test_killed_after_verification_before_publishing(self, v1_db):
        result = _run_child(v1_db, "database._publish = lambda *a: os._exit(3)")
        assert result.returncode == 3
        assert scalar(v1_db, "PRAGMA user_version") == 1
        assert _published(v1_db) == []

        SqliteHistory(v1_db).watch_entries()
        assert len(_published(v1_db)) == 1
        assert _temps(v1_db) == []


@pytest.mark.unit
class TestFailedBackup:
    def test_copy_that_differs_from_the_source_is_rejected(self, v1_db, monkeypatch):
        real_copy = database._copy_database

        def lossy_copy(source, target):
            real_copy(source, target)
            with closing(sqlite3.connect(target)) as conn:
                conn.execute("DELETE FROM downloaded_episode WHERE series_id = '3'")
                conn.commit()

        monkeypatch.setattr(database, "_copy_database", lossy_copy)
        before = v1_db.read_bytes()
        with pytest.raises(HistoryBackupError) as info:
            SqliteHistory(v1_db).watch_entries()
        assert info.value.code == "history.backup_failed"
        assert v1_db.read_bytes() == before
        assert _published(v1_db) == [] and _temps(v1_db) == []

    def test_unwritable_copy_is_fail_closed(self, v1_db, monkeypatch):
        def no_space(temp, final):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(database, "_publish", no_space)
        with pytest.raises(HistoryBackupError):
            SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 1
        assert _temps(v1_db) == []

        monkeypatch.undo()
        SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 2

    def test_existing_backup_is_neither_trusted_nor_overwritten(self, v1_db):
        stale = v1_db.with_name(f"{v1_db.name}.schema-1.20000101T000000000000Z.bak")
        stale.write_bytes(b"not a database")
        SqliteHistory(v1_db).watch_entries()
        assert stale.read_bytes() == b"not a database"
        fresh = [p for p in _published(v1_db) if p != stale]
        assert len(fresh) == 1
        _assert_valid_v1_copy(fresh[0])


@pytest.mark.unit
class TestConcurrentStart:
    def test_two_instances_make_one_backup_and_one_migration(self, v1_db):
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        seen: list[int] = []

        def start() -> None:
            try:
                barrier.wait()
                seen.append(len(SqliteHistory(v1_db).watch_entries()))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == []
        assert seen == [3, 3]
        assert scalar(v1_db, "PRAGMA user_version") == 2
        assert len(_published(v1_db)) == 1


@pytest.mark.unit
class TestDurability:
    @pytest.mark.skipif(os.name != "posix", reason="Windows has no directory fsync")
    def test_backup_directory_is_synced_after_the_rename(self, v1_db, monkeypatch):
        synced: list[str] = []
        real_open, real_fsync = os.open, os.fsync
        opened: dict[int, str] = {}

        def tracking_open(path, flags, *args):
            fd = real_open(path, flags, *args)
            opened[fd] = str(path)
            return fd

        def tracking_fsync(fd):
            synced.append(opened.get(fd, "file"))
            return real_fsync(fd)

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "fsync", tracking_fsync)
        SqliteHistory(v1_db).watch_entries()
        assert str(v1_db.parent) in synced


@pytest.mark.unit
class TestRollback:
    def test_error_after_sqlite_rolled_back_itself_is_not_hidden(self, tmp_path):
        db = database.Database(tmp_path / "history.db")
        with pytest.raises(RuntimeError, match="disk full"), db.write() as conn:
            conn.execute("ROLLBACK")
            raise RuntimeError("disk full")
        with db.write():
            pass
