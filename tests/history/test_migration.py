import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing

import pytest

from alt_ani_cli import legacy_history
from alt_ani_core.errors import HistoryVersionError
from alt_ani_core.history import migrations
from alt_ani_core.history.migrations import SCHEMA_VERSION
from alt_ani_core.history.sqlite import SqliteHistory

from .state_support import FIXTURES, open_state, scalar

V1_TABLES = ["downloaded_episode", "meta", "series"]
V2_TABLES = ["download_job", "episode", "offline_item", "preferences", "watch_state"]


def _v1(path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript((FIXTURES / "history_v1.sql").read_text(encoding="utf-8"))
        conn.commit()


def _rows(path, table: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()


def _tables(path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


@pytest.fixture
def v1_db(tmp_path):
    path = tmp_path / "state" / "history.db"
    path.parent.mkdir()
    _v1(path)
    return path


@pytest.fixture
def v1_snapshot(v1_db):
    return {table: _rows(v1_db, table) for table in V1_TABLES}


@pytest.mark.unit
class TestUpgradeFromSchema1:
    def test_schema_version_is_2(self):
        assert SCHEMA_VERSION == 2

    def test_existing_rows_and_meta_are_kept_verbatim(self, v1_db, v1_snapshot):
        SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 2
        assert {table: _rows(v1_db, table) for table in V1_TABLES} == v1_snapshot

    def test_nothing_is_derived_from_the_cursor_or_download_records(self, v1_db):
        SqliteHistory(v1_db).watch_entries()
        for table in ("episode", "watch_state", "download_job", "offline_item"):
            assert scalar(v1_db, f"SELECT COUNT(*) FROM {table}") == 0, table

    def test_preferences_start_at_the_agreed_defaults(self, v1_db):
        prefs = open_state(v1_db).preferences.get()
        assert (prefs.audio, prefs.subs, prefs.quality.encode()) == ("any", "any", "best")
        assert (prefs.download_network.value, prefs.auto_alternatives, prefs.max_active_downloads) == ("wifi_only", False, 1)

    def test_cli_history_reads_the_same_after_the_upgrade(self, v1_db):
        store = SqliteHistory(v1_db)
        assert [(e.series.id, e.last_ep) for e in store.watch_entries()] == [("2", 12.5), ("1", 4.0), ("4", 0.0)]
        assert {e.series.id: e.episodes for e in store.download_entries()} == {
            "3": frozenset({0.5, 7.0}),
            "1": frozenset({1.0, 2.0}),
        }

    def test_new_state_has_no_known_position_for_old_cursor_episodes(self, v1_db):
        state = open_state(v1_db)
        assert state.watch.for_series("1") == []
        assert state.queue.jobs() == []
        assert state.library.items() == []

    def test_legacy_import_metadata_survives_and_still_prevents_reimport(self, v1_db):
        store = SqliteHistory(v1_db)
        assert store.meta(legacy_history.META_SHA) == "0123456789abcdef"
        assert store.meta(legacy_history.META_STATUS) == "done"
        assert (store.meta(legacy_history.META_IMPORTED), store.meta(legacy_history.META_SKIPPED)) == ("3", "1")


@pytest.mark.unit
class TestFreshDatabase:
    def test_empty_file_goes_straight_to_2_without_a_backup(self, tmp_path):
        path = tmp_path / "history.db"
        open_state(path).preferences.get()
        assert scalar(path, "PRAGMA user_version") == 2
        assert _tables(path) == sorted(V1_TABLES + V2_TABLES)
        assert list(tmp_path.glob("*.bak")) == []


@pytest.mark.unit
class TestInterruptedMigration:
    def test_failing_statement_leaves_schema_1_and_a_retry_succeeds(self, v1_db, v1_snapshot, monkeypatch):
        steps = list(migrations.MIGRATIONS[2])
        broken = [*steps[:3], "CREATE TABLE broken (", *steps[3:]]
        monkeypatch.setitem(migrations.MIGRATIONS, 2, tuple(broken))
        with pytest.raises(sqlite3.OperationalError):
            SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 1
        assert _tables(v1_db) == V1_TABLES
        monkeypatch.setitem(migrations.MIGRATIONS, 2, tuple(steps))
        SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 2
        assert {table: _rows(v1_db, table) for table in V1_TABLES} == v1_snapshot

    def test_process_killed_mid_migration_rolls_back_on_next_open(self, v1_db, v1_snapshot):
        script = textwrap.dedent(
            """
            import os, sys
            from pathlib import Path
            from alt_ani_core.history import migrations
            from alt_ani_core.history.sqlite import SqliteHistory
            steps = migrations.MIGRATIONS[2]
            class DieMidway:
                def __iter__(self):
                    for index, statement in enumerate(steps):
                        if index == 4:
                            os._exit(3)
                        yield statement
            migrations.MIGRATIONS[2] = DieMidway()
            SqliteHistory(Path(sys.argv[1])).watch_entries()
            """
        )
        result = subprocess.run([sys.executable, "-c", script, str(v1_db)], timeout=60, check=False)
        assert result.returncode == 3
        assert scalar(v1_db, "PRAGMA user_version") == 1
        SqliteHistory(v1_db).watch_entries()
        assert scalar(v1_db, "PRAGMA user_version") == 2
        assert {table: _rows(v1_db, table) for table in V1_TABLES} == v1_snapshot


@pytest.mark.unit
class TestNewerSchema:
    def test_schema_3_is_refused_and_untouched(self, tmp_path):
        path = tmp_path / "history.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        before = path.read_bytes()
        with pytest.raises(HistoryVersionError):
            open_state(path).queue.jobs()
        assert path.read_bytes() == before
        assert list(tmp_path.glob("*.bak")) == []


@pytest.mark.unit
class TestSchemaMatchesTheModel:
    def test_open_job_index_uses_the_terminal_states(self):
        from alt_ani_core.history.queue import OPEN_JOB

        assert migrations._OPEN_JOB == OPEN_JOB

    def test_job_state_check_lists_every_state(self, tmp_path):
        from alt_ani_core.state import JobState

        path = tmp_path / "history.db"
        open_state(path).queue.jobs()
        ddl = scalar(path, "SELECT sql FROM sqlite_master WHERE name = 'download_job'")
        assert all(f"'{state.value}'" in ddl for state in JobState)
