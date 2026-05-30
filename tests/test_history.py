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
