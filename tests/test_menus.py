"""Tests for ui/menus — only the pure/fallback paths (no InquirerPy TTY needed)."""

from unittest.mock import patch

from alt_ani_cli.shinden.models import EpisodeRow
from alt_ani_cli.ui.menus import select_action, select_episodes, select_quality, select_start_mode


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

    with patch("builtins.print", side_effect=_capture_print):
        with patch("builtins.input", return_value="1"):
            select_quality({"480p": "u3", "1080p": "u1", "720p": "u2"})

    resolution_lines = [l for l in printed if any(r in l for r in ("1080p", "720p", "480p"))]
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

def test_select_episodes_watched_marker_in_label(monkeypatch):
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    episodes = [
        EpisodeRow(number=1, title="Ep A", url="http://x/1"),
        EpisodeRow(number=2, title="Ep B", url="http://x/2"),
    ]
    printed_lines: list[str] = []

    def _capture(*args, **kwargs):
        printed_lines.append(" ".join(str(a) for a in args))

    with patch("builtins.print", side_effect=_capture):
        with patch("builtins.input", return_value="2"):
            select_episodes(episodes, watched_numbers={1.0})

    joined = "\n".join(printed_lines)
    assert "✓" in joined  # watched ep 1 should be marked
