"""Tests for ui/menus — only the pure/fallback paths (no InquirerPy TTY needed)."""

from unittest.mock import patch

from alt_ani_cli.shinden.models import EpisodeRow, RelatedSeries, SeriesHit, SeriesMetadata, SeriesRef
from alt_ani_cli.ui.menus import (
    _pick_related,
    _run_keyed_picker,
    _run_simple_picker,
    _sorted_by_date_desc,
    select_action,
    select_episodes,
    select_quality,
    select_series,
    select_series_from_history,
    select_start_mode,
)


def test_select_quality_empty_returns_best():
    assert select_quality({}) == "best"


def test_select_quality_fallback_picks_best(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="1"):
        result = select_quality({"1080p": "u1", "720p": "u2"})
    assert result == "best"


def test_select_quality_fallback_picks_1080p(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="2"):
        result = select_quality({"1080p": "u1", "720p": "u2"})
    assert result == "1080p"


def test_select_quality_fallback_picks_worst(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    qualities = {"1080p": "u1", "720p": "u2"}
    last_idx = str(len(qualities) + 2)  # best + 2 resolutions + worst = idx 4
    with patch("builtins.input", return_value=last_idx):
        result = select_quality(qualities)
    assert result == "worst"


def test_select_quality_sorted_descending(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    labels: list[str] = []
    original_print = __builtins__["print"] if isinstance(__builtins__, dict) else print

    printed: list[str] = []

    def _capture_print(*args, **kwargs):
        printed.append(" ".join(str(a) for a in args))

    with patch("builtins.print", side_effect=_capture_print), patch("builtins.input", return_value="1"):
        select_quality({"480p": "u3", "1080p": "u1", "720p": "u2"})

    resolution_lines = [line for line in printed if any(r in line for r in ("1080p", "720p", "480p"))]
    assert resolution_lines[0].find("1080p") < resolution_lines[1].find("720p") or \
           "1080p" in resolution_lines[0]


def test_select_start_mode_no_history_fallback(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="1"):
        result = select_start_mode(has_history=False)
    assert result == "search"


def test_select_start_mode_quit_fallback(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="3"):
        result = select_start_mode(has_history=False)
    assert result == "quit"


def test_select_start_mode_resume_with_history_fallback(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="2"):
        result = select_start_mode(has_history=True, history_count=3)
    assert result == "resume"


def test_select_action_play_fallback(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="1"):
        assert select_action() == "play"


def test_select_action_download_fallback(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="2"):
        assert select_action() == "download"


def test_select_action_debug_fallback(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value="3"):
        assert select_action() == "debug"


def test_select_quality_empty_enter_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value=""):
        assert select_quality({"1080p": "u1"}) is None


def test_select_start_mode_empty_enter_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value=""):
        assert select_start_mode(has_history=False) is None


def test_select_action_empty_enter_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("builtins.input", return_value=""):
        assert select_action() is None


def test_select_episodes_empty_enter_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    episodes = [EpisodeRow(number=1, title="Ep 1", url="http://x/1")]
    with patch("builtins.input", return_value=""):
        assert select_episodes(episodes) is None

def test_run_simple_picker_fallback_returns_selected_item(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    items = ["alfa", "beta", "gamma"]
    with patch("builtins.input", return_value="2"):
        result = _run_simple_picker(items, lambda x: x, prompt="Wybierz", instruction="Enter=ok")
    assert result == "beta"


def test_run_simple_picker_fallback_empty_enter_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    items = ["alfa"]
    with patch("builtins.input", return_value=""):
        assert _run_simple_picker(items, lambda x: x, prompt="Wybierz", instruction="") is None


def test_select_series_fallback_no_metadata_shows_label_without_date(monkeypatch, capsys):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    hits = [SeriesHit(id="1", slug="test", title="Test Anime", url="https://shinden.pl/series/1-test", series_type="TV")]
    with patch("builtins.input", return_value="1"):
        result = select_series(hits)
    assert result == hits[0]
    captured = capsys.readouterr()
    assert "Test Anime" in captured.out
    assert "(id:1)" in captured.out


def test_select_series_fallback_metadata_shows_date_in_label(monkeypatch, capsys):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    hits = [SeriesHit(id="234", slug="ikkitousen", title="Ikkitousen", url="https://shinden.pl/series/234-ikkitousen", series_type="TV")]
    meta = {"234": SeriesMetadata(air_date="30.07.2003", air_date_sort=(2003, 7, 30), description="", tags=(), related=())}
    with patch("builtins.input", return_value="1"):
        result = select_series(hits, metadata=meta)
    assert result == hits[0]
    captured = capsys.readouterr()
    assert "30.07.2003" in captured.out


def test_select_series_fallback_empty_enter_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    hits = [SeriesHit(id="1", slug="test", title="Test", url="https://shinden.pl/series/1-test")]
    with patch("builtins.input", return_value=""):
        assert select_series(hits) is None


def test_select_series_from_history_after_refactor(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    ref = SeriesRef(id="1", slug="test", title="Test Anime", url="https://shinden.pl/series/1-test")
    entries = [(ref, 3.0), (ref, 5.0)]
    with patch("builtins.input", return_value="2"):
        result = select_series_from_history(entries)
    assert result == entries[1]


def test_select_episodes_watched_marker_in_label(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    episodes = [
        EpisodeRow(number=1, title="Ep A", url="http://x/1"),
        EpisodeRow(number=2, title="Ep B", url="http://x/2"),
    ]
    printed_lines: list[str] = []

    def _capture(*args, **kwargs):
        printed_lines.append(" ".join(str(a) for a in args))

    with patch("builtins.print", side_effect=_capture), patch("builtins.input", return_value="2"):
        select_episodes(episodes, watched_numbers={1.0})

    joined = "\n".join(printed_lines)
    assert "✓" in joined  # watched ep 1 should be marked


def test_sorted_by_date_desc_orders_newest_first():
    hits = [
        SeriesHit(id="1", slug="a", title="A", url="", series_type=""),
        SeriesHit(id="2", slug="b", title="B", url="", series_type=""),
        SeriesHit(id="3", slug="c", title="C", url="", series_type=""),
    ]
    meta = {
        "1": SeriesMetadata(air_date="01.01.2020", air_date_sort=(2020, 1, 1), description="", tags=(), related=()),
        "2": SeriesMetadata(air_date="01.06.2023", air_date_sort=(2023, 6, 1), description="", tags=(), related=()),
        "3": SeriesMetadata(air_date=None, air_date_sort=None, description="", tags=(), related=()),
    }
    result = _sorted_by_date_desc(hits, meta)
    assert [h.id for h in result] == ["2", "1", "3"]


def test_sorted_by_date_desc_no_dates_does_not_crash():
    hits = [
        SeriesHit(id="1", slug="a", title="A", url="", series_type=""),
        SeriesHit(id="2", slug="b", title="B", url="", series_type=""),
    ]
    result = _sorted_by_date_desc(hits, {})
    assert len(result) == 2


def test_pick_related_empty_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    with patch("alt_ani_cli.ui.progress.warn"), patch("builtins.input", return_value=""):
        result = _pick_related(())
    assert result is None


def test_pick_related_fallback_picks_item(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    items = (RelatedSeries(id="10", slug="sequel", title="Sequel Anime", url="https://shinden.pl/series/10-sequel", relation="Sequel"),)
    with patch("builtins.input", return_value="1"):
        result = _pick_related(items)
    assert result == items[0]


def test_pick_related_fallback_empty_enter_returns_none(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    items = (RelatedSeries(id="10", slug="sequel", title="Sequel", url="https://shinden.pl/series/10-sequel", relation="Sequel"),)
    with patch("builtins.input", return_value=""):
        result = _pick_related(items)
    assert result is None


class TestRunKeyedPicker:
    def test_returns_selected_key(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        options = [("play", "Odtwórz"), ("download", "Pobierz")]
        with patch("builtins.input", return_value="2"):
            result = _run_keyed_picker(options, prompt="Akcja", instruction="", fallback_invalid="Zły wybór")
        assert result == "download"

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        options = [("play", "Odtwórz")]
        with patch("builtins.input", return_value=""):
            result = _run_keyed_picker(options, prompt="Akcja", instruction="", fallback_invalid="Zły wybór")
        assert result is None

    def test_invalid_input_retries_and_succeeds(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        options = [("a", "Alpha"), ("b", "Beta")]
        printed = []
        inputs = iter(["xyz", "1"])
        with patch("builtins.input", side_effect=inputs), patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            result = _run_keyed_picker(options, prompt="Wybierz", instruction="", fallback_invalid="INVALID")
        assert result == "a"
        assert any("INVALID" in line for line in printed)


class TestSelectActionFallback:
    def test_play_is_first_option(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            result = select_action()
        assert result == "play"

    def test_download_is_second_option(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            result = select_action()
        assert result == "download"

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert select_action() is None


class TestSelectStartModeFallback:
    def test_search_is_first_option(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            result = select_start_mode(has_history=False)
        assert result == "search"

    def test_resume_with_count_in_label(self, monkeypatch, capsys):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            select_start_mode(has_history=True, history_count=5)
        captured = capsys.readouterr()
        assert "5" in captured.out

    def test_quit_without_history(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="3"):
            result = select_start_mode(has_history=False)
        assert result == "quit"
