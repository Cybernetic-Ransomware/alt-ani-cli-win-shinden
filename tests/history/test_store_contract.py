import itertools

import pytest
from alt_ani_core.history import sqlite as sqlite_history
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.history.store import DownloadEntry, WatchEntry
from alt_ani_core.models import SeriesRef
from alt_ani_core.testing import InMemoryHistory


def _ref(id: str = "123", title: str = "Test Anime") -> SeriesRef:
    return SeriesRef(id=id, slug="test-anime", title=title, url=f"https://shinden.pl/series/{id}-test-anime")


@pytest.fixture
def clock():
    """Strictly increasing ISO timestamps, so ordering never depends on how fast two writes follow each other."""
    ticks = itertools.count(1)
    return lambda: f"2024-01-01T00:00:{next(ticks):02d}+00:00"


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path, monkeypatch, clock):
    if request.param == "sqlite":
        monkeypatch.setattr(sqlite_history, "_now", clock)
        return SqliteHistory(tmp_path / "history.db")
    return InMemoryHistory(clock=clock)


@pytest.mark.unit
class TestWatchHistory:
    def test_new_store_is_empty(self, store):
        assert store.watch_entries() == []
        assert store.download_entries() == []

    def test_record_and_list(self, store):
        store.record_watch(_ref(), 5.0)
        assert store.watch_entries() == [WatchEntry(_ref(), 5.0, "2024-01-01T00:00:01+00:00")]

    def test_record_again_overwrites_the_cursor(self, store):
        store.record_watch(_ref(), 3.0)
        store.record_watch(_ref(), 7.0)
        entries = store.watch_entries()
        assert [(e.series.id, e.last_ep) for e in entries] == [("123", 7.0)]
        assert entries[0].updated_at == "2024-01-01T00:00:02+00:00"

    def test_cursor_may_move_backwards(self, store):
        store.record_watch(_ref(), 7.0)
        store.record_watch(_ref(), 2.0)
        assert store.watch_entries()[0].last_ep == 2.0

    def test_fractional_episode_number_round_trips(self, store):
        store.record_watch(_ref(), 5.5)
        entry = store.watch_entries()[0]
        assert entry.last_ep == 5.5
        assert isinstance(entry.last_ep, float)

    def test_integer_episode_is_stored_as_float(self, store):
        store.record_watch(_ref(), 7)
        assert isinstance(store.watch_entries()[0].last_ep, float)

    def test_listed_newest_first(self, store):
        for series_id in ("1", "2", "3"):
            store.record_watch(_ref(id=series_id), 1.0)
        assert [e.series.id for e in store.watch_entries()] == ["3", "2", "1"]

    def test_rewatching_moves_a_series_to_the_front(self, store):
        for series_id in ("1", "2", "3"):
            store.record_watch(_ref(id=series_id), 1.0)
        store.record_watch(_ref(id="1"), 2.0)
        assert [e.series.id for e in store.watch_entries()] == ["1", "3", "2"]

    def test_record_refreshes_series_metadata(self, store):
        store.record_watch(_ref(title="Old Title"), 1.0)
        store.record_watch(
            SeriesRef(id="123", slug="new-slug", title="New Title", url="https://shinden.pl/series/123-new-slug"), 2.0
        )
        assert store.watch_entries()[0].series == SeriesRef(
            id="123", slug="new-slug", title="New Title", url="https://shinden.pl/series/123-new-slug"
        )

    def test_clear(self, store):
        store.record_watch(_ref(), 1.0)
        store.clear()
        assert store.watch_entries() == []

    def test_usable_after_clear(self, store):
        store.record_watch(_ref(), 1.0)
        store.clear()
        store.record_watch(_ref(), 4.0)
        assert [e.last_ep for e in store.watch_entries()] == [4.0]


@pytest.mark.unit
class TestDownloadHistory:
    def test_record_creates_download_only_entry(self, store):
        store.record_download(_ref(), 1.0)
        assert store.download_entries() == [DownloadEntry(_ref(), frozenset({1.0}), "2024-01-01T00:00:01+00:00")]
        assert store.watch_entries() == []

    def test_accumulates_without_duplicates(self, store):
        for number in (1.0, 2.0, 4.0, 2.0, 1.0):
            store.record_download(_ref(), number)
        entries = store.download_entries()
        assert len(entries) == 1
        assert entries[0].episodes == frozenset({1.0, 2.0, 4.0})

    def test_fractional_episode_number_round_trips(self, store):
        store.record_download(_ref(), 5.5)
        store.record_download(_ref(), 5)
        assert store.download_entries()[0].episodes == frozenset({5.0, 5.5})

    def test_listed_newest_first(self, store):
        for series_id in ("1", "2", "3"):
            store.record_download(_ref(id=series_id), 1.0)
        store.record_download(_ref(id="1"), 2.0)
        assert [e.series.id for e in store.download_entries()] == ["1", "3", "2"]

    def test_order_ignores_watch_activity(self, store):
        store.record_download(_ref(id="1"), 1.0)
        store.record_download(_ref(id="2"), 1.0)
        store.record_watch(_ref(id="1"), 1.0)
        assert [e.series.id for e in store.download_entries()] == ["2", "1"]

    def test_watch_order_ignores_download_activity(self, store):
        store.record_watch(_ref(id="1"), 1.0)
        store.record_watch(_ref(id="2"), 1.0)
        store.record_download(_ref(id="1"), 1.0)
        assert [e.series.id for e in store.watch_entries()] == ["2", "1"]

    def test_watch_list_skips_download_only_entries(self, store):
        store.record_download(_ref(id="1", title="Downloaded"), 1.0)
        store.record_watch(_ref(id="2", title="Watched"), 3.0)
        assert [e.series.id for e in store.watch_entries()] == ["2"]

    def test_download_list_skips_watch_only_entries(self, store):
        store.record_download(_ref(id="1", title="Downloaded"), 1.0)
        store.record_watch(_ref(id="2", title="Watched"), 3.0)
        assert [e.series.id for e in store.download_entries()] == ["1"]

    def test_watch_record_preserves_download_fields(self, store):
        for number in (1.0, 2.0, 3.0):
            store.record_download(_ref(), number)
        before = store.download_entries()[0]
        store.record_watch(_ref(), 1.0)
        assert store.download_entries() == [before]
        assert store.watch_entries()[0].last_ep == 1.0

    def test_download_record_preserves_watch_fields(self, store):
        store.record_watch(_ref(), 5.0)
        before = store.watch_entries()[0]
        store.record_download(_ref(), 6.0)
        assert store.watch_entries() == [before]

    def test_download_record_refreshes_series_metadata(self, store):
        store.record_watch(_ref(title="Old Title"), 1.0)
        store.record_download(_ref(title="New Title"), 1.0)
        assert store.watch_entries()[0].series.title == "New Title"
        assert store.download_entries()[0].series.title == "New Title"

    def test_watch_record_refreshes_metadata_of_downloads(self, store):
        store.record_download(_ref(title="Old Title"), 1.0)
        store.record_watch(_ref(title="New Title"), 1.0)
        assert store.download_entries()[0].series.title == "New Title"

    def test_clear_removes_both_history_kinds(self, store):
        store.record_watch(_ref(id="1"), 1.0)
        store.record_download(_ref(id="2"), 1.0)
        store.clear()
        assert store.watch_entries() == []
        assert store.download_entries() == []

    def test_cleared_downloads_do_not_come_back_with_the_series(self, store):
        store.record_download(_ref(), 1.0)
        store.clear()
        store.record_download(_ref(), 2.0)
        assert store.download_entries()[0].episodes == frozenset({2.0})
