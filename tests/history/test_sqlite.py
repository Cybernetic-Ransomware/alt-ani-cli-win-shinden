import sqlite3
import threading
from contextlib import closing

import pytest

from alt_ani_core.errors import HistoryVersionError
from alt_ani_core.history.sqlite import SCHEMA_VERSION, SqliteHistory
from alt_ani_core.history.store import LegacySeries
from alt_ani_core.models import SeriesRef


def _ref(id: str = "123", title: str = "Test Anime") -> SeriesRef:
    return SeriesRef(id=id, slug="test-anime", title=title, url=f"https://shinden.pl/series/{id}-test-anime")


def _legacy(id: str = "123", **fields) -> LegacySeries:
    return LegacySeries(series=_ref(id=id, title=fields.pop("title", "Legacy Title")), **fields)


def _scalar(path, sql: str):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchone()[0]


@pytest.fixture
def store(tmp_path) -> SqliteHistory:
    return SqliteHistory(tmp_path / "history.db")


@pytest.fixture
def connections(monkeypatch) -> list[tuple[sqlite3.Connection, int]]:
    """Every connection the store opens, with the foreign_keys pragma it was handed out with."""
    opened: list[tuple[sqlite3.Connection, int]] = []
    real_connect = SqliteHistory._connect

    def connect(self) -> sqlite3.Connection:
        conn = real_connect(self)
        opened.append((conn, conn.execute("PRAGMA foreign_keys").fetchone()[0]))
        return conn

    monkeypatch.setattr(SqliteHistory, "_connect", connect)
    return opened


def _exercise(store: SqliteHistory) -> None:
    store.record_watch(_ref(), 1.0)
    store.record_download(_ref(), 1.0)
    store.watch_entries()
    store.download_entries()
    store.set_meta({"k": "v"})
    store.meta("k")
    store.import_legacy([_legacy(id="9", last_ep=1.0)], {"m": "1"})
    store.clear()


@pytest.mark.unit
class TestConnections:
    def test_foreign_keys_enabled_on_every_connection(self, store, connections):
        _exercise(store)
        assert len(connections) >= 8
        assert {pragma for _, pragma in connections} == {1}

    def test_every_operation_opens_its_own_connection_and_closes_it(self, store, connections):
        store.record_watch(_ref(), 1.0)
        after_write = len(connections)
        store.watch_entries()
        store.watch_entries()
        assert after_write >= 1
        assert len(connections) >= after_write + 2
        assert len({id(conn) for conn, _ in connections}) == len(connections)
        for conn, _ in connections:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_usable_from_another_thread(self, store):
        store.record_watch(_ref(id="1"), 1.0)
        errors: list[BaseException] = []
        seen: list[list[str]] = []

        def work() -> None:
            try:
                store.record_watch(_ref(id="2"), 2.0)
                store.record_download(_ref(id="2"), 2.0)
                seen.append(sorted(e.series.id for e in store.watch_entries()))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        assert errors == []
        assert seen == [["1", "2"]]
        assert sorted(e.series.id for e in store.watch_entries()) == ["1", "2"]
        assert [e.series.id for e in store.download_entries()] == ["2"]

    def test_deleting_a_series_cascades_to_downloaded_episodes(self, store):
        store.record_download(_ref(id="1"), 1.0)
        store.record_download(_ref(id="1"), 2.0)
        store.record_download(_ref(id="2"), 1.0)
        with closing(store._connect()) as conn:
            conn.execute("DELETE FROM series WHERE id = '1'")
        assert _scalar(store.path, "SELECT COUNT(*) FROM downloaded_episode WHERE series_id = '1'") == 0
        assert _scalar(store.path, "SELECT COUNT(*) FROM downloaded_episode WHERE series_id = '2'") == 1

    def test_clear_leaves_no_orphaned_downloaded_episodes(self, store):
        store.record_download(_ref(), 1.0)
        store.clear()
        assert _scalar(store.path, "SELECT COUNT(*) FROM downloaded_episode") == 0

    def test_orphan_downloaded_episode_is_rejected(self, store):
        store.record_watch(_ref(), 1.0)
        with closing(store._connect()) as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO downloaded_episode (series_id, episode) VALUES ('missing', 1.0)")


@pytest.mark.unit
class TestSchema:
    def test_constructor_does_not_touch_the_disk(self, tmp_path):
        store = SqliteHistory(tmp_path / "state" / "history.db")
        assert not (tmp_path / "state").exists()
        assert store.path == tmp_path / "state" / "history.db"

    def test_first_use_creates_missing_parent_directory(self, tmp_path):
        store = SqliteHistory(tmp_path / "state" / "nested" / "history.db")
        assert store.watch_entries() == []
        assert store.path.is_file()

    @pytest.mark.parametrize("first_use", ["read", "write"])
    def test_user_version_is_set(self, store, first_use):
        if first_use == "read":
            store.watch_entries()
        else:
            store.record_watch(_ref(), 1.0)
        assert _scalar(store.path, "PRAGMA user_version") == SCHEMA_VERSION
        assert SCHEMA_VERSION >= 1

    def test_data_survives_reopening(self, store):
        store.record_watch(_ref(), 5.5)
        store.record_download(_ref(), 2.0)
        reopened = SqliteHistory(store.path)
        assert [(e.series, e.last_ep) for e in reopened.watch_entries()] == [(_ref(), 5.5)]
        assert reopened.download_entries()[0].episodes == frozenset({2.0})
        assert _scalar(store.path, "PRAGMA user_version") == SCHEMA_VERSION


def _newer_db(path) -> bytes:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE future (payload TEXT)")
        conn.execute("INSERT INTO future VALUES ('from a newer release')")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    return path.read_bytes()


@pytest.mark.unit
class TestNewerSchema:
    @pytest.mark.parametrize(
        "operation",
        [
            lambda s: s.record_watch(_ref(), 1.0),
            lambda s: s.record_download(_ref(), 1.0),
            lambda s: s.watch_entries(),
            lambda s: s.download_entries(),
            lambda s: s.clear(),
            lambda s: s.meta("k"),
            lambda s: s.set_meta({"k": "v"}),
            lambda s: s.import_legacy([_legacy(last_ep=1.0)], {"k": "v"}),
        ],
        ids=["record_watch", "record_download", "watch_entries", "download_entries", "clear", "meta", "set_meta", "import"],
    )
    def test_raises_and_leaves_the_file_unmodified(self, store, operation):
        before = _newer_db(store.path)
        with pytest.raises(HistoryVersionError) as info:
            operation(store)
        assert info.value.code == "history.newer_schema"
        assert info.value.params == {"found": SCHEMA_VERSION + 1, "supported": SCHEMA_VERSION}
        assert store.path.read_bytes() == before
        assert _scalar(store.path, "PRAGMA user_version") == SCHEMA_VERSION + 1
        assert _scalar(store.path, "SELECT payload FROM future") == "from a newer release"
        assert _scalar(store.path, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'series'") == 0

    def test_keeps_raising_on_later_calls(self, store):
        _newer_db(store.path)
        for _ in range(2):
            with pytest.raises(HistoryVersionError):
                store.watch_entries()


@pytest.mark.unit
class TestMeta:
    def test_missing_key_is_none(self, store):
        assert store.meta("absent") is None

    def test_set_and_overwrite(self, store):
        store.set_meta({"a": "1", "b": "2"})
        store.set_meta({"a": "3"})
        assert (store.meta("a"), store.meta("b")) == ("3", "2")

    def test_clear_keeps_meta(self, store):
        store.record_watch(_ref(), 1.0)
        store.set_meta({"legacy_json_sha": "abc"})
        store.clear()
        assert store.watch_entries() == []
        assert store.meta("legacy_json_sha") == "abc"


@pytest.mark.unit
class TestImportLegacy:
    def test_carries_identifiers_numbers_and_timestamps_verbatim(self, store):
        store.import_legacy(
            [
                _legacy(
                    last_ep=5.5,
                    watch_updated_at="2024-01-01T00:00:00+00:00",
                    downloaded=(1.0, 2.5),
                    download_updated_at="2023-06-01T12:00:00+00:00",
                )
            ],
            {"done": "yes"},
        )
        (watch,) = store.watch_entries()
        (download,) = store.download_entries()
        assert (watch.series, watch.last_ep, watch.updated_at) == (
            _ref(title="Legacy Title"),
            5.5,
            "2024-01-01T00:00:00+00:00",
        )
        assert (download.series, download.episodes, download.updated_at) == (
            _ref(title="Legacy Title"),
            frozenset({1.0, 2.5}),
            "2023-06-01T12:00:00+00:00",
        )
        assert store.meta("done") == "yes"

    def test_download_only_record_stays_out_of_the_watch_list(self, store):
        store.import_legacy([_legacy(downloaded=(1.0,), download_updated_at="2024-01-01T00:00:00+00:00")], {})
        assert store.watch_entries() == []
        assert store.download_entries()[0].episodes == frozenset({1.0})

    def test_watch_only_record_stays_out_of_the_download_list(self, store):
        store.import_legacy([_legacy(last_ep=7.0, watch_updated_at="2024-01-01T00:00:00+00:00")], {})
        assert [e.last_ep for e in store.watch_entries()] == [7.0]
        assert store.download_entries() == []

    def test_sorted_by_legacy_timestamps_newest_first(self, store):
        store.import_legacy(
            [
                _legacy(id="1", last_ep=1.0, watch_updated_at="2024-01-01T00:00:00+00:00"),
                _legacy(id="2", last_ep=1.0, watch_updated_at="2024-06-15T00:00:00+00:00"),
                _legacy(id="3", last_ep=1.0, watch_updated_at="2023-12-31T00:00:00+00:00"),
            ],
            {},
        )
        assert [e.series.id for e in store.watch_entries()] == ["2", "1", "3"]

    def test_downloads_sorted_by_their_own_timestamp(self, store):
        store.import_legacy(
            [
                _legacy(
                    id="1",
                    last_ep=1.0,
                    watch_updated_at="2025-01-01T00:00:00+00:00",
                    downloaded=(1.0,),
                    download_updated_at="2024-01-01T00:00:00+00:00",
                ),
                _legacy(id="2", downloaded=(1.0,), download_updated_at="2024-06-15T00:00:00+00:00"),
                _legacy(id="3", downloaded=(1.0,), download_updated_at="2023-12-31T00:00:00+00:00"),
            ],
            {},
        )
        assert [e.series.id for e in store.download_entries()] == ["2", "1", "3"]

    def test_missing_timestamp_sorts_last_and_reads_as_empty(self, store):
        store.import_legacy(
            [
                _legacy(id="2", last_ep=1.0),
                _legacy(id="1", last_ep=1.0, watch_updated_at="2024-01-01T00:00:00+00:00"),
            ],
            {},
        )
        entries = store.watch_entries()
        assert [e.series.id for e in entries] == ["1", "2"]
        assert entries[1].updated_at == ""

    def test_existing_row_wins_on_watch_fields_and_metadata(self, store):
        store.record_watch(_ref(title="Current Title"), 9.0)
        current = store.watch_entries()[0]
        store.import_legacy([_legacy(last_ep=3.0, watch_updated_at="2020-01-01T00:00:00+00:00")], {})
        assert store.watch_entries() == [current]

    def test_existing_newer_looking_legacy_timestamp_still_loses(self, store):
        store.record_watch(_ref(), 9.0)
        current = store.watch_entries()[0]
        store.import_legacy([_legacy(last_ep=3.0, watch_updated_at="2999-01-01T00:00:00+00:00")], {})
        assert store.watch_entries() == [current]

    def test_download_only_row_gains_the_legacy_watch_cursor(self, store):
        store.record_download(_ref(title="Current Title"), 4.0)
        store.import_legacy([_legacy(last_ep=3.0, watch_updated_at="2020-01-01T00:00:00+00:00")], {})
        (watch,) = store.watch_entries()
        assert (watch.series.title, watch.last_ep, watch.updated_at) == ("Current Title", 3.0, "2020-01-01T00:00:00+00:00")

    def test_downloaded_episodes_are_unioned(self, store):
        store.record_download(_ref(), 1.0)
        store.record_download(_ref(), 2.0)
        current_at = store.download_entries()[0].updated_at
        store.import_legacy([_legacy(downloaded=(2.0, 3.0, 5.5), download_updated_at="2020-01-01T00:00:00+00:00")], {})
        (download,) = store.download_entries()
        assert download.episodes == frozenset({1.0, 2.0, 3.0, 5.5})
        assert download.updated_at == current_at

    def test_watch_only_row_gains_legacy_downloads_and_timestamp(self, store):
        store.record_watch(_ref(), 9.0)
        store.import_legacy([_legacy(downloaded=(1.0,), download_updated_at="2020-01-01T00:00:00+00:00")], {})
        (download,) = store.download_entries()
        assert (download.episodes, download.updated_at) == (frozenset({1.0}), "2020-01-01T00:00:00+00:00")
        assert store.watch_entries()[0].last_ep == 9.0

    def test_importing_twice_changes_nothing(self, store):
        records = [_legacy(last_ep=3.0, watch_updated_at="2020-01-01T00:00:00+00:00", downloaded=(1.0, 2.0))]
        store.import_legacy(records, {"n": "1"})
        watch, downloads = store.watch_entries(), store.download_entries()
        store.import_legacy(records, {"n": "2"})
        assert (store.watch_entries(), store.download_entries()) == (watch, downloads)
        assert store.meta("n") == "2"

    def test_empty_import_still_records_meta(self, store):
        store.import_legacy([], {"done": "yes"})
        assert store.meta("done") == "yes"
        assert store.watch_entries() == []

    def test_failure_mid_import_leaves_no_partial_rows_and_no_meta(self, store):
        assert store.watch_entries() == []

        def records():
            yield _legacy(id="1", last_ep=1.0, downloaded=(1.0,))
            raise RuntimeError("source went away")

        with pytest.raises(RuntimeError):
            store.import_legacy(records(), {"done": "yes"})
        assert store.watch_entries() == []
        assert store.download_entries() == []
        assert store.meta("done") is None
        assert _scalar(store.path, "SELECT COUNT(*) FROM series") == 0
        assert _scalar(store.path, "SELECT COUNT(*) FROM downloaded_episode") == 0

    def test_failure_mid_import_keeps_what_was_there_before(self, store):
        store.record_watch(_ref(id="1"), 9.0)
        store.record_download(_ref(id="1"), 1.0)
        store.set_meta({"done": "before"})
        watch, downloads = store.watch_entries(), store.download_entries()

        def records():
            yield _legacy(id="1", last_ep=1.0, downloaded=(2.0, 3.0))
            yield _legacy(id="2", last_ep=1.0)
            raise sqlite3.OperationalError("disk I/O error")

        with pytest.raises(sqlite3.OperationalError):
            store.import_legacy(records(), {"done": "after"})
        assert (store.watch_entries(), store.download_entries()) == (watch, downloads)
        assert store.meta("done") == "before"

    def test_failing_meta_write_rolls_back_the_imported_rows(self, store):
        assert store.watch_entries() == []
        with pytest.raises(sqlite3.Error):
            store.import_legacy([_legacy(last_ep=1.0)], {"done": None})
        assert store.watch_entries() == []
        assert store.meta("done") is None

    def test_read_works_after_the_very_first_write_was_rolled_back(self, store):
        def records():
            yield _legacy(last_ep=1.0)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            store.import_legacy(records(), {"done": "yes"})
        assert store.watch_entries() == []
        assert store.meta("done") is None

    def test_store_is_usable_after_a_failed_import(self, store):
        def records():
            yield _legacy(last_ep=1.0)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            store.import_legacy(records(), {})
        store.import_legacy([_legacy(last_ep=2.0)], {"done": "yes"})
        assert [e.last_ep for e in store.watch_entries()] == [2.0]
        assert store.meta("done") == "yes"
