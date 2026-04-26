import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from alt_ani_cli.shinden.models import SeriesRef


def _make_ref(id="123", title="Test Anime") -> SeriesRef:
    return SeriesRef(id=id, slug="test-anime", title=title, url="https://shinden.pl/series/123-test-anime")


def test_upsert_and_list(tmp_path):
    with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "h.json"), \
         patch("alt_ani_cli.history.STATE_DIR", tmp_path):
        import alt_ani_cli.history as h
        ref = _make_ref()
        h.upsert(ref, last_ep=5.0)
        entries = h.list_all()
        assert len(entries) == 1
        assert entries[0][0].title == "Test Anime"
        assert entries[0][1] == 5.0


def test_upsert_overwrite(tmp_path):
    with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "h.json"), \
         patch("alt_ani_cli.history.STATE_DIR", tmp_path):
        import alt_ani_cli.history as h
        ref = _make_ref()
        h.upsert(ref, last_ep=3.0)
        h.upsert(ref, last_ep=7.0)
        entries = h.list_all()
        assert len(entries) == 1
        assert entries[0][1] == 7.0


def test_clear(tmp_path):
    with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "h.json"), \
         patch("alt_ani_cli.history.STATE_DIR", tmp_path):
        import alt_ani_cli.history as h
        h.upsert(_make_ref(), last_ep=1.0)
        h.clear()
        assert h.list_all() == []


def test_load_missing_file(tmp_path):
    with patch("alt_ani_cli.history.HISTORY_FILE", tmp_path / "nonexistent.json"), \
         patch("alt_ani_cli.history.STATE_DIR", tmp_path):
        import alt_ani_cli.history as h
        assert h.list_all() == []


def test_load_corrupted_json(tmp_path):
    hf = tmp_path / "h.json"
    hf.write_text("not valid json", encoding="utf-8")
    with patch("alt_ani_cli.history.HISTORY_FILE", hf), \
         patch("alt_ani_cli.history.STATE_DIR", tmp_path):
        import alt_ani_cli.history as h
        assert h.list_all() == []


def test_atomic_write(tmp_path):
    """Ensure tmp file is replaced atomically — no partial write visible."""
    hf = tmp_path / "h.json"
    with patch("alt_ani_cli.history.HISTORY_FILE", hf), \
         patch("alt_ani_cli.history.STATE_DIR", tmp_path):
        import alt_ani_cli.history as h
        h.upsert(_make_ref(), last_ep=2.0)
        assert hf.exists()
        assert not (tmp_path / "h.tmp").exists()
