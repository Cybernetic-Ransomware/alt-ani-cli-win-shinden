"""Tests for JSON-backed watch history."""

import json
from unittest.mock import patch

import pytest

from alt_ani_cli.shinden.models import SeriesRef


def _make_ref(id="123", title="Test Anime") -> SeriesRef:
    return SeriesRef(id=id, slug="test-anime", title=title, url="https://shinden.pl/series/123-test-anime")


@pytest.mark.unit
class TestHistory:
    def test_upsert_and_list(self, tmp_path):
        with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "h.json"), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            ref = _make_ref()
            h.upsert(ref, last_ep=5.0)
            entries = h.list_all()
            assert len(entries) == 1
            assert entries[0][0].title == "Test Anime"
            assert entries[0][1] == 5.0

    def test_upsert_overwrite(self, tmp_path):
        with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "h.json"), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            ref = _make_ref()
            h.upsert(ref, last_ep=3.0)
            h.upsert(ref, last_ep=7.0)
            entries = h.list_all()
            assert len(entries) == 1
            assert entries[0][1] == 7.0

    def test_clear(self, tmp_path):
        with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "h.json"), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            h.upsert(_make_ref(), last_ep=1.0)
            h.clear()
            assert h.list_all() == []

    def test_load_missing_file(self, tmp_path):
        with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "nonexistent.json"), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            assert h.list_all() == []

    def test_load_corrupted_json(self, tmp_path):
        hf = tmp_path / "h.json"
        hf.write_text("not valid json", encoding="utf-8")
        with patch("alt_ani_cli.history.HISTORY_FILE", hf), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            assert h.list_all() == []

    def test_atomic_write(self, tmp_path):
        """Ensure tmp file is replaced atomically — no partial write visible."""
        hf = tmp_path / "h.json"
        with patch("alt_ani_cli.history.HISTORY_FILE", hf), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            h.upsert(_make_ref(), last_ep=2.0)
            assert hf.exists()
            assert not (tmp_path / "h.tmp").exists()

    def test_list_sorted_by_updated_at_descending(self, tmp_path):
        hf = tmp_path / "h.json"
        hf.write_text(json.dumps({
            "version": 1,
            "series": {
                "1": {"title": "Alpha", "slug": "alpha", "url": "http://x/1", "last_ep": 1.0, "updated_at": "2024-01-01T00:00:00+00:00"},
                "2": {"title": "Beta",  "slug": "beta",  "url": "http://x/2", "last_ep": 1.0, "updated_at": "2024-06-15T00:00:00+00:00"},
                "3": {"title": "Gamma", "slug": "gamma", "url": "http://x/3", "last_ep": 1.0, "updated_at": "2023-12-31T00:00:00+00:00"},
            },
        }), encoding="utf-8")
        with patch("alt_ani_cli.history.HISTORY_FILE", hf), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            entries = h.list_all()
        assert [e[0].id for e in entries] == ["2", "1", "3"]

    def test_list_missing_updated_at_sorts_last(self, tmp_path):
        hf = tmp_path / "h.json"
        hf.write_text(json.dumps({
            "version": 1,
            "series": {
                "1": {"title": "WithDate",    "slug": "a", "url": "http://x/1", "last_ep": 1.0, "updated_at": "2024-01-01T00:00:00+00:00"},
                "2": {"title": "WithoutDate", "slug": "b", "url": "http://x/2", "last_ep": 1.0},
            },
        }), encoding="utf-8")
        with patch("alt_ani_cli.history.HISTORY_FILE", hf), \
             patch("alt_ani_cli.history.STATE_DIR", tmp_path):
            import alt_ani_cli.history as h
            entries = h.list_all()
        assert entries[0][0].id == "1"
        assert entries[1][0].id == "2"


def _history(tmp_path, data: dict | None = None):
    hf = tmp_path / "h.json"
    if data is not None:
        hf.write_text(json.dumps(data), encoding="utf-8")
    return hf, patch("alt_ani_cli.history.HISTORY_FILE", hf), patch("alt_ani_cli.history.STATE_DIR", tmp_path)


@pytest.mark.unit
class TestDownloadHistory:
    def test_legacy_watch_only_entry_still_lists(self, tmp_path):
        _, p_file, p_dir = _history(tmp_path, {
            "version": 1,
            "series": {"7": {"title": "Old", "slug": "old", "url": "http://x/7", "last_ep": 7, "updated_at": "2024-01-01T00:00:00+00:00"}},
        })
        with p_file, p_dir:
            import alt_ani_cli.history as h
            entries = h.list_all()
            downloads = h.list_downloads()
        assert [(e[0].id, e[1]) for e in entries] == [("7", 7.0)]
        assert downloads == []

    def test_record_download_creates_download_only_entry(self, tmp_path):
        hf, p_file, p_dir = _history(tmp_path)
        with p_file, p_dir:
            import alt_ani_cli.history as h
            h.record_download(_make_ref(), 1.0)
            downloads = h.list_downloads()
        assert downloads == [(_make_ref(), frozenset({1.0}))]
        entry = json.loads(hf.read_text(encoding="utf-8"))["series"]["123"]
        assert entry["downloaded_eps"] == [1.0]
        assert "download_updated_at" in entry
        assert "last_ep" not in entry
        assert "updated_at" not in entry

    def test_record_download_accumulates_without_duplicates(self, tmp_path):
        hf, p_file, p_dir = _history(tmp_path)
        with p_file, p_dir:
            import alt_ani_cli.history as h
            for n in (1.0, 2.0, 4.0, 2.0, 1.0):
                h.record_download(_make_ref(), n)
            downloads = h.list_downloads()
        assert downloads[0][1] == frozenset({1.0, 2.0, 4.0})
        assert json.loads(hf.read_text(encoding="utf-8"))["series"]["123"]["downloaded_eps"] == [1.0, 2.0, 4.0]

    def test_list_downloads_sorted_by_download_updated_at_descending(self, tmp_path):
        _, p_file, p_dir = _history(tmp_path, {
            "version": 1,
            "series": {
                "1": {"title": "A", "slug": "a", "url": "http://x/1", "downloaded_eps": [1.0], "download_updated_at": "2024-01-01T00:00:00+00:00",
                      "last_ep": 1.0, "updated_at": "2025-01-01T00:00:00+00:00"},
                "2": {"title": "B", "slug": "b", "url": "http://x/2", "downloaded_eps": [1.0], "download_updated_at": "2024-06-15T00:00:00+00:00"},
                "3": {"title": "C", "slug": "c", "url": "http://x/3", "downloaded_eps": [1.0], "download_updated_at": "2023-12-31T00:00:00+00:00"},
            },
        })
        with p_file, p_dir:
            import alt_ani_cli.history as h
            downloads = h.list_downloads()
        assert [d[0].id for d in downloads] == ["2", "1", "3"]

    def test_list_all_skips_download_only_entries(self, tmp_path):
        _, p_file, p_dir = _history(tmp_path)
        with p_file, p_dir:
            import alt_ani_cli.history as h
            h.record_download(_make_ref(id="1", title="Downloaded"), 1.0)
            h.upsert(_make_ref(id="2", title="Watched"), last_ep=3.0)
            entries = h.list_all()
        assert [e[0].id for e in entries] == ["2"]

    def test_watch_upsert_preserves_download_fields(self, tmp_path):
        hf, p_file, p_dir = _history(tmp_path)
        with p_file, p_dir:
            import alt_ani_cli.history as h
            for n in (1.0, 2.0, 3.0):
                h.record_download(_make_ref(), n)
            before = json.loads(hf.read_text(encoding="utf-8"))["series"]["123"]["download_updated_at"]
            h.upsert(_make_ref(), last_ep=1.0)
            entry = json.loads(hf.read_text(encoding="utf-8"))["series"]["123"]
        assert entry["downloaded_eps"] == [1.0, 2.0, 3.0]
        assert entry["download_updated_at"] == before
        assert entry["last_ep"] == 1.0

    def test_record_download_preserves_watch_fields(self, tmp_path):
        hf, p_file, p_dir = _history(tmp_path)
        with p_file, p_dir:
            import alt_ani_cli.history as h
            h.upsert(_make_ref(), last_ep=5.0)
            before = json.loads(hf.read_text(encoding="utf-8"))["series"]["123"]["updated_at"]
            h.record_download(_make_ref(), 6.0)
            entry = json.loads(hf.read_text(encoding="utf-8"))["series"]["123"]
            entries = h.list_all()
        assert entry["last_ep"] == 5.0
        assert entry["updated_at"] == before
        assert entries[0][1] == 5.0

    def test_record_download_refreshes_series_metadata(self, tmp_path):
        _, p_file, p_dir = _history(tmp_path)
        with p_file, p_dir:
            import alt_ani_cli.history as h
            h.upsert(_make_ref(title="Old Title"), last_ep=1.0)
            h.record_download(_make_ref(title="New Title"), 1.0)
            entries = h.list_all()
        assert entries[0][0].title == "New Title"

    def test_clear_removes_both_history_kinds(self, tmp_path):
        _, p_file, p_dir = _history(tmp_path)
        with p_file, p_dir:
            import alt_ani_cli.history as h
            h.upsert(_make_ref(id="1"), last_ep=1.0)
            h.record_download(_make_ref(id="2"), 1.0)
            h.clear()
            assert h.list_all() == []
            assert h.list_downloads() == []

    def test_version_1_file_without_download_fields_accepts_new_writes(self, tmp_path):
        hf, p_file, p_dir = _history(tmp_path, {
            "version": 1,
            "series": {"123": {"title": "Test Anime", "slug": "test-anime", "url": "http://x", "last_ep": 4.0,
                               "updated_at": "2024-01-01T00:00:00+00:00"}},
        })
        with p_file, p_dir:
            import alt_ani_cli.history as h
            assert h.list_downloads() == []
            h.record_download(_make_ref(), 5.0)
            entries = h.list_all()
            downloads = h.list_downloads()
        data = json.loads(hf.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert entries[0][1] == 4.0
        assert downloads[0][1] == frozenset({5.0})
