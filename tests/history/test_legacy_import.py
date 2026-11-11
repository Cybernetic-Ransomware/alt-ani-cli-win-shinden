import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.models import SeriesRef

from alt_ani_cli import legacy_history
from alt_ani_cli.legacy_history import ImportReport, ImportStatus, import_legacy

_WATCHED = {
    "title": "Watched Show",
    "slug": "watched-show",
    "url": "https://shinden.pl/series/7-watched-show",
    "last_ep": 7,
    "updated_at": "2024-01-01T00:00:00+00:00",
}
_BOTH = {
    "title": "Both Show",
    "slug": "both-show",
    "url": "https://shinden.pl/series/8-both-show",
    "last_ep": 5.5,
    "updated_at": "2024-06-15T10:20:30.123456+00:00",
    "downloaded_eps": [1.0, 2.0, 5.5],
    "download_updated_at": "2024-03-01T00:00:00+00:00",
}
_DOWNLOADED = {
    "title": "Downloaded Show",
    "slug": "downloaded-show",
    "url": "https://shinden.pl/series/9-downloaded-show",
    "downloaded_eps": [3.0],
    "download_updated_at": "2023-12-31T00:00:00+00:00",
}
_VALID = {"7": _WATCHED, "8": _BOTH, "9": _DOWNLOADED}


def _dump(series: dict) -> bytes:
    return json.dumps({"version": 1, "series": series}, ensure_ascii=False, indent=2).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _meta(store: SqliteHistory) -> dict[str, str | None]:
    keys = (
        legacy_history.META_SHA,
        legacy_history.META_STATUS,
        legacy_history.META_IMPORTED,
        legacy_history.META_SKIPPED,
    )
    return {key: store.meta(key) for key in keys}


_NO_META = {
    legacy_history.META_SHA: None,
    legacy_history.META_STATUS: None,
    legacy_history.META_IMPORTED: None,
    legacy_history.META_SKIPPED: None,
}


@pytest.fixture
def store(tmp_path) -> SqliteHistory:
    return SqliteHistory(tmp_path / "state" / "history.db")


@pytest.fixture
def source(tmp_path) -> Path:
    (tmp_path / "state").mkdir()
    return tmp_path / "state" / "history.json"


@pytest.fixture
def backup(source) -> Path:
    return source.with_name("history.json.pre-sqlite.bak")


@pytest.mark.unit
class TestNothingToImport:
    def test_missing_file_returns_none(self, store, source, backup):
        assert import_legacy(store, source) is None
        assert not backup.exists()
        assert not store.path.exists()

    def test_directory_in_place_of_the_file_returns_none(self, store, source):
        source.mkdir()
        assert import_legacy(store, source) is None


@pytest.mark.unit
class TestValidFile:
    def test_reports_done_with_counts(self, store, source):
        source.write_bytes(_dump(_VALID))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)

    def test_identifiers_numbers_and_timestamps_carried_verbatim(self, store, source):
        source.write_bytes(_dump(_VALID))
        import_legacy(store, source)
        assert [(e.series, e.last_ep, e.updated_at) for e in store.watch_entries()] == [
            (
                SeriesRef(id="8", slug="both-show", title="Both Show", url="https://shinden.pl/series/8-both-show"),
                5.5,
                "2024-06-15T10:20:30.123456+00:00",
            ),
            (
                SeriesRef(id="7", slug="watched-show", title="Watched Show", url="https://shinden.pl/series/7-watched-show"),
                7.0,
                "2024-01-01T00:00:00+00:00",
            ),
        ]
        assert [(e.series, e.episodes, e.updated_at) for e in store.download_entries()] == [
            (
                SeriesRef(id="8", slug="both-show", title="Both Show", url="https://shinden.pl/series/8-both-show"),
                frozenset({1.0, 2.0, 5.5}),
                "2024-03-01T00:00:00+00:00",
            ),
            (
                SeriesRef(
                    id="9", slug="downloaded-show", title="Downloaded Show", url="https://shinden.pl/series/9-downloaded-show"
                ),
                frozenset({3.0}),
                "2023-12-31T00:00:00+00:00",
            ),
        ]

    def test_records_sha_status_and_counts_in_meta(self, store, source):
        raw = _dump(_VALID)
        source.write_bytes(raw)
        import_legacy(store, source)
        assert _meta(store) == {
            legacy_history.META_SHA: _sha(raw),
            legacy_history.META_STATUS: "done",
            legacy_history.META_IMPORTED: "3",
            legacy_history.META_SKIPPED: "0",
        }

    def test_source_bytes_unchanged_and_backup_created(self, store, source, backup):
        raw = _dump(_VALID)
        source.write_bytes(raw)
        import_legacy(store, source)
        assert source.read_bytes() == raw
        assert backup.read_bytes() == raw
        assert sorted(p.name for p in source.parent.iterdir()) == ["history.db", "history.json", backup.name]

    def test_missing_metadata_falls_back_to_the_series_id(self, store, source):
        source.write_bytes(_dump({"42": {"last_ep": 3}}))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=1, skipped=0)
        (entry,) = store.watch_entries()
        assert entry.series == SeriesRef(id="42", slug="", title="42", url="")
        assert (entry.last_ep, entry.updated_at) == (3.0, "")

    def test_empty_series_map_is_done_with_nothing_imported(self, store, source):
        source.write_bytes(_dump({}))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=0, skipped=0)
        assert store.meta(legacy_history.META_STATUS) == "done"
        assert import_legacy(store, source) is None

    def test_imported_file_without_download_fields_accepts_new_writes(self, store, source):
        source.write_bytes(_dump({"7": _WATCHED}))
        import_legacy(store, source)
        assert store.download_entries() == []
        ref = SeriesRef(id="7", slug="watched-show", title="Watched Show", url="https://shinden.pl/series/7-watched-show")
        store.record_download(ref, 5.0)
        assert [(e.series.id, e.last_ep) for e in store.watch_entries()] == [("7", 7.0)]
        assert [(e.series.id, e.episodes) for e in store.download_entries()] == [("7", frozenset({5.0}))]


@pytest.mark.unit
class TestIdempotence:
    def test_second_run_with_unchanged_file_returns_none(self, store, source):
        source.write_bytes(_dump(_VALID))
        import_legacy(store, source)
        watch, downloads, meta = store.watch_entries(), store.download_entries(), _meta(store)
        assert import_legacy(store, source) is None
        assert (store.watch_entries(), store.download_entries(), _meta(store)) == (watch, downloads, meta)

    def test_unchanged_file_does_not_undo_later_history(self, store, source):
        source.write_bytes(_dump({"7": _WATCHED}))
        import_legacy(store, source)
        ref = SeriesRef(id="7", slug="watched-show", title="Watched Show", url="https://shinden.pl/series/7-watched-show")
        store.record_watch(ref, 12.0)
        assert import_legacy(store, source) is None
        assert store.watch_entries()[0].last_ep == 12.0

    def test_unchanged_file_is_recognised_by_a_new_store_object(self, store, source):
        source.write_bytes(_dump(_VALID))
        import_legacy(store, source)
        assert import_legacy(SqliteHistory(store.path), source) is None

    def test_cleared_history_is_not_reimported_from_an_unchanged_file(self, store, source):
        source.write_bytes(_dump(_VALID))
        import_legacy(store, source)
        store.clear()
        assert import_legacy(store, source) is None
        assert store.watch_entries() == []
        assert store.download_entries() == []

    def test_backup_is_created_only_once(self, store, source, backup):
        first = _dump({"7": _WATCHED})
        source.write_bytes(first)
        import_legacy(store, source)
        source.write_bytes(_dump(_VALID))
        import_legacy(store, source)
        assert backup.read_bytes() == first

    def test_existing_backup_is_never_overwritten(self, store, source, backup):
        backup.write_bytes(b"kept from an earlier install")
        source.write_bytes(_dump(_VALID))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)
        assert backup.read_bytes() == b"kept from an earlier install"


@pytest.mark.unit
class TestChangedFile:
    def test_new_content_is_imported_again(self, store, source):
        source.write_bytes(_dump({"7": _WATCHED}))
        import_legacy(store, source)
        changed = _dump(_VALID)
        source.write_bytes(changed)
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)
        assert sorted(e.series.id for e in store.watch_entries()) == ["7", "8"]
        assert store.meta(legacy_history.META_SHA) == _sha(changed)
        assert source.read_bytes() == changed

    def test_database_wins_on_watch_fields(self, store, source):
        source.write_bytes(_dump({"7": _WATCHED}))
        import_legacy(store, source)
        ref = SeriesRef(id="7", slug="watched-show", title="Current Title", url="https://shinden.pl/series/7-watched-show")
        store.record_watch(ref, 12.0)
        current = store.watch_entries()
        source.write_bytes(
            _dump({"7": {**_WATCHED, "last_ep": 9, "updated_at": "2999-01-01T00:00:00+00:00", "title": "Stale"}})
        )
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=1, skipped=0)
        assert store.watch_entries() == current

    def test_downloaded_episodes_are_unioned(self, store, source):
        source.write_bytes(_dump({"9": _DOWNLOADED}))
        import_legacy(store, source)
        ref = SeriesRef(
            id="9", slug="downloaded-show", title="Downloaded Show", url="https://shinden.pl/series/9-downloaded-show"
        )
        store.record_download(ref, 4.0)
        current_at = store.download_entries()[0].updated_at
        source.write_bytes(_dump({"9": {**_DOWNLOADED, "downloaded_eps": [3.0, 5.0, 6.5]}}))
        import_legacy(store, source)
        (entry,) = store.download_entries()
        assert entry.episodes == frozenset({3.0, 4.0, 5.0, 6.5})
        assert entry.updated_at == current_at

    def test_changed_file_after_clear_is_imported(self, store, source):
        source.write_bytes(_dump({"7": _WATCHED}))
        import_legacy(store, source)
        store.clear()
        source.write_bytes(_dump({"7": {**_WATCHED, "last_ep": 8}}))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=1, skipped=0)
        assert store.watch_entries()[0].last_ep == 8.0


_INVALID_ENTRIES = {
    "entry_not_a_mapping": "just a string",
    "entry_is_a_list": [1, 2],
    "last_ep_is_text": {"title": "A", "last_ep": "7"},
    "last_ep_is_null": {"title": "A", "last_ep": None},
    "last_ep_is_bool": {"title": "A", "last_ep": True},
    "last_ep_is_nan": {"title": "A", "last_ep": float("nan")},
    "last_ep_is_infinite": {"title": "A", "last_ep": float("inf")},
    "downloads_not_a_list": {"title": "A", "downloaded_eps": "1,2"},
    "downloads_hold_text": {"title": "A", "last_ep": 1, "downloaded_eps": [1.0, "two"]},
    "downloads_hold_bool": {"title": "A", "downloaded_eps": [False]},
    "neither_watch_nor_download": {"title": "A", "slug": "a", "url": "http://x/a"},
    "empty_download_list_only": {"title": "A", "downloaded_eps": []},
}


@pytest.mark.unit
class TestInvalidEntries:
    @pytest.mark.parametrize("name", list(_INVALID_ENTRIES))
    def test_invalid_entry_is_skipped_and_counted(self, store, source, name):
        source.write_bytes(_dump({"7": _WATCHED, "bad": _INVALID_ENTRIES[name]}))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=1, skipped=1)
        assert [e.series.id for e in store.watch_entries()] == ["7"]
        assert store.download_entries() == []

    def test_empty_series_id_is_skipped(self, store, source):
        source.write_bytes(_dump({"": _WATCHED, "7": _WATCHED}))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=1, skipped=1)
        assert [e.series.id for e in store.watch_entries()] == ["7"]

    def test_counts_are_recorded_and_the_file_is_not_retried(self, store, source):
        source.write_bytes(_dump({**_VALID, **_INVALID_ENTRIES}))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=len(_INVALID_ENTRIES))
        assert store.meta(legacy_history.META_IMPORTED) == "3"
        assert store.meta(legacy_history.META_SKIPPED) == str(len(_INVALID_ENTRIES))
        assert import_legacy(store, source) is None

    def test_non_text_metadata_falls_back_without_skipping(self, store, source):
        source.write_bytes(_dump({"7": {"title": 5, "slug": None, "url": ["x"], "last_ep": 2, "updated_at": 123}}))
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=1, skipped=0)
        (entry,) = store.watch_entries()
        assert entry.series == SeriesRef(id="7", slug="", title="7", url="")
        assert entry.updated_at == ""

    def test_integer_episode_numbers_become_floats(self, store, source):
        source.write_bytes(_dump({"7": {"last_ep": 7, "downloaded_eps": [1, 2]}}))
        import_legacy(store, source)
        assert store.watch_entries()[0].last_ep == 7.0
        assert store.download_entries()[0].episodes == frozenset({1.0, 2.0})


_CORRUPT_FILES = {
    "not_json": b"not valid json",
    "empty_file": b"",
    "truncated_json": b'{"version": 1, "series": {"7": {"last_ep"',
    "not_utf8": b"\xff\xfe\x00{",
    "top_level_list": b"[]",
    "top_level_scalar": b"42",
    "no_series_key": b'{"version": 1}',
    "series_is_a_list": b'{"version": 1, "series": []}',
    "series_is_null": b'{"version": 1, "series": null}',
}


@pytest.mark.unit
class TestCorruptFile:
    @pytest.mark.parametrize("name", list(_CORRUPT_FILES))
    def test_reported_and_recorded_with_its_sha(self, store, source, backup, name):
        raw = _CORRUPT_FILES[name]
        source.write_bytes(raw)
        assert import_legacy(store, source) == ImportReport(ImportStatus.CORRUPT)
        assert _meta(store) == {**_NO_META, legacy_history.META_SHA: _sha(raw), legacy_history.META_STATUS: "corrupt"}
        assert store.watch_entries() == []
        assert store.download_entries() == []
        assert source.read_bytes() == raw
        assert backup.read_bytes() == raw

    def test_not_retried_while_the_file_is_unchanged(self, store, source):
        source.write_bytes(b"not valid json")
        import_legacy(store, source)
        assert import_legacy(store, source) is None
        assert import_legacy(store, source) is None

    def test_retried_once_the_file_changes(self, store, source):
        source.write_bytes(b"not valid json")
        import_legacy(store, source)
        fixed = _dump(_VALID)
        source.write_bytes(fixed)
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)
        assert len(store.watch_entries()) == 2
        assert store.meta(legacy_history.META_SHA) == _sha(fixed)
        assert store.meta(legacy_history.META_STATUS) == "done"

    def test_still_corrupt_after_a_change_is_reported_again(self, store, source):
        source.write_bytes(b"not valid json")
        import_legacy(store, source)
        source.write_bytes(b"still not json")
        assert import_legacy(store, source) == ImportReport(ImportStatus.CORRUPT)
        assert store.meta(legacy_history.META_SHA) == _sha(b"still not json")

    def test_corrupt_replacement_keeps_previously_imported_history(self, store, source):
        source.write_bytes(_dump(_VALID))
        import_legacy(store, source)
        watch, downloads = store.watch_entries(), store.download_entries()
        source.write_bytes(b"not valid json")
        assert import_legacy(store, source) == ImportReport(ImportStatus.CORRUPT)
        assert (store.watch_entries(), store.download_entries()) == (watch, downloads)


def _read_bytes_fails_for(monkeypatch, target: Path) -> None:
    real_read_bytes = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == target:
            raise PermissionError(13, "sharing violation", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


@pytest.mark.unit
class TestTransientFailure:
    def test_unreadable_source_is_unavailable_and_retried(self, store, source, backup, monkeypatch):
        raw = _dump(_VALID)
        source.write_bytes(raw)
        with monkeypatch.context() as broken:
            _read_bytes_fails_for(broken, source)
            assert import_legacy(store, source) == ImportReport(ImportStatus.UNAVAILABLE)
        assert not backup.exists()
        assert not store.path.exists()
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)
        assert len(store.watch_entries()) == 2
        assert source.read_bytes() == raw

    def test_failing_backup_write_is_unavailable_and_retried(self, store, source, backup, monkeypatch):
        source.write_bytes(_dump(_VALID))
        real_write_bytes = Path.write_bytes

        def write_bytes(self: Path, data) -> int:
            if self == backup:
                raise OSError(28, "No space left on device")
            return real_write_bytes(self, data)

        with monkeypatch.context() as broken:
            broken.setattr(Path, "write_bytes", write_bytes)
            assert import_legacy(store, source) == ImportReport(ImportStatus.UNAVAILABLE)
        assert _meta(store) == _NO_META
        assert store.watch_entries() == []
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)
        assert backup.read_bytes() == source.read_bytes()

    def test_locked_database_on_the_sha_lookup_is_unavailable_and_retried(self, store, source, monkeypatch):
        source.write_bytes(_dump(_VALID))
        with monkeypatch.context() as broken:
            broken.setattr(store, "meta", lambda key: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
            assert import_legacy(store, source) == ImportReport(ImportStatus.UNAVAILABLE)
        assert _meta(store) == _NO_META
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)

    def test_store_failure_during_import_records_nothing_and_is_retried(self, store, source, monkeypatch):
        raw = _dump(_VALID)
        source.write_bytes(raw)

        def locked(records, meta) -> None:
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as broken:
            broken.setattr(store, "import_legacy", locked)
            assert import_legacy(store, source) == ImportReport(ImportStatus.UNAVAILABLE)
        assert _meta(store) == _NO_META
        assert store.watch_entries() == []
        assert store.download_entries() == []
        assert source.read_bytes() == raw
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)
        assert len(store.watch_entries()) == 2
        assert store.meta(legacy_history.META_STATUS) == "done"

    def test_real_write_failure_inside_the_transaction_leaves_no_partial_rows(self, store, source, monkeypatch):
        source.write_bytes(_dump(_VALID))

        def failing_meta(conn, values) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        with monkeypatch.context() as broken:
            broken.setattr(SqliteHistory, "_set_meta", staticmethod(failing_meta))
            assert import_legacy(store, source) == ImportReport(ImportStatus.UNAVAILABLE)
        assert _meta(store) == _NO_META
        assert store.watch_entries() == []
        assert store.download_entries() == []
        assert import_legacy(store, source) == ImportReport(ImportStatus.DONE, imported=3, skipped=0)

    def test_failure_recording_a_corrupt_file_is_unavailable_and_retried(self, store, source, monkeypatch):
        source.write_bytes(b"not valid json")

        def locked(values) -> None:
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as broken:
            broken.setattr(store, "set_meta", locked)
            assert import_legacy(store, source) == ImportReport(ImportStatus.UNAVAILABLE)
        assert _meta(store) == _NO_META
        assert import_legacy(store, source) == ImportReport(ImportStatus.CORRUPT)
