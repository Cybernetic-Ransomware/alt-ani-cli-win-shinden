"""Tests for ui/menus — only the pure/fallback paths (no InquirerPy TTY needed)."""

from unittest.mock import patch

import pytest

from alt_ani_cli.models import PlayerSource
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, RelatedSeries, SeriesHit, SeriesRef
from alt_ani_cli.ui.menus import (
    _origin_similar,
    _run_keyed_picker,
    _run_simple_picker,
    _source_host,
    confirm,
    format_player_source,
    pick_related,
    select_action,
    select_episodes,
    select_player_once,
    select_quality,
    select_series_from_history,
    select_series_once,
    select_start_mode,
)


@pytest.mark.unit
class TestSelectQuality:
    def test_empty_returns_best(self):
        assert select_quality({}) == "best"

    def test_fallback_picks_best(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            assert select_quality({"1080p": "u1", "720p": "u2"}) == "best"

    def test_fallback_picks_1080p(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            assert select_quality({"1080p": "u1", "720p": "u2"}) == "1080p"

    def test_fallback_picks_worst(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        qualities = {"1080p": "u1", "720p": "u2"}
        last_idx = str(len(qualities) + 2)
        with patch("builtins.input", return_value=last_idx):
            assert select_quality(qualities) == "worst"

    def test_fallback_sorted_descending(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        printed: list[str] = []
        with (
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
            patch("builtins.input", return_value="1"),
        ):
            select_quality({"480p": "u3", "1080p": "u1", "720p": "u2"})
        resolution_lines = [line for line in printed if any(r in line for r in ("1080p", "720p", "480p"))]
        assert resolution_lines[0].find("1080p") < resolution_lines[1].find("720p") or "1080p" in resolution_lines[0]

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert select_quality({"1080p": "u1"}) is None


@pytest.mark.unit
class TestSelectStartMode:
    def test_search_is_first_option(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            assert select_start_mode(has_history=False) == "search"

    def test_resume_returns_resume(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            assert select_start_mode(has_history=True, history_count=3) == "resume"

    def test_quit_without_history(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="3"):
            assert select_start_mode(has_history=False) == "quit"

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert select_start_mode(has_history=False) is None


@pytest.mark.unit
class TestSelectAction:
    def test_play_is_first_option(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            assert select_action() == "play"

    def test_download_is_second_option(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            assert select_action() == "download"

    def test_debug_fallback(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="3"):
            assert select_action() == "debug"

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert select_action() is None


@pytest.mark.unit
class TestSelectEpisodes:
    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        episodes = [EpisodeRow(number=1, title="Ep 1", url="http://x/1")]
        with patch("builtins.input", return_value=""):
            assert select_episodes(episodes) is None

    def test_fallback_ignores_default_index(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        episodes = [
            EpisodeRow(number=1, title="Ep 1", url="http://x/1"),
            EpisodeRow(number=2, title="Ep 2", url="http://x/2"),
        ]
        with patch("builtins.input", return_value="1"):
            assert select_episodes(episodes, multi=True, default_index=1) == [episodes[0]]


@pytest.mark.unit
class TestSelectSeriesOnce:
    def test_fallback_pick_returns_pick_signal(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        hits = [SeriesHit(id="1", slug="test", title="Test Anime", url="https://shinden.pl/series/1-test", series_type="TV")]
        with patch("builtins.input", return_value="1"):
            assert select_series_once(hits) == ("pick", hits[0])

    def test_fallback_empty_enter_returns_back_signal(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        hits = [SeriesHit(id="1", slug="test", title="Test", url="https://shinden.pl/series/1-test")]
        with patch("builtins.input", return_value=""):
            assert select_series_once(hits) == ("back", None)

    def test_from_history_after_refactor(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        ref = SeriesRef(id="1", slug="test", title="Test Anime", url="https://shinden.pl/series/1-test")
        entries = [(ref, 3.0), (ref, 5.0)]
        with patch("builtins.input", return_value="2"):
            assert select_series_from_history(entries) == entries[1]


@pytest.mark.unit
class TestPickRelated:
    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("alt_ani_cli.ui.progress.warn"), patch("builtins.input", return_value=""):
            assert pick_related(()) is None

    def test_fallback_picks_item(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        items = (RelatedSeries(id="10", slug="sequel", title="Sequel Anime", url="https://shinden.pl/series/10-sequel", relation="Sequel"),)
        with patch("builtins.input", return_value="1"):
            assert pick_related(items) == items[0]

    def test_fallback_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        items = (RelatedSeries(id="10", slug="sequel", title="Sequel", url="https://shinden.pl/series/10-sequel", relation="Sequel"),)
        with patch("builtins.input", return_value=""):
            assert pick_related(items) is None


@pytest.mark.unit
class TestRunSimplePicker:
    def test_fallback_returns_selected_item(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        items = ["alfa", "beta", "gamma"]
        with patch("builtins.input", return_value="2"):
            assert _run_simple_picker(items, lambda x: x, prompt="Wybierz", instruction="Enter=ok") == "beta"

    def test_fallback_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert _run_simple_picker(["alfa"], lambda x: x, prompt="Wybierz", instruction="") is None


@pytest.mark.unit
class TestRunKeyedPicker:
    def test_returns_selected_key(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        options = [("play", "Odtwórz"), ("download", "Pobierz")]
        with patch("builtins.input", return_value="2"):
            assert _run_keyed_picker(options, prompt="Akcja", instruction="", fallback_invalid="Zły wybór") == "download"

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        options = [("play", "Odtwórz")]
        with patch("builtins.input", return_value=""):
            assert _run_keyed_picker(options, prompt="Akcja", instruction="", fallback_invalid="Zły wybór") is None

    def test_invalid_input_retries_and_succeeds(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        options = [("a", "Alpha"), ("b", "Beta")]
        printed = []
        inputs = iter(["xyz", "1"])
        with (
            patch("builtins.input", side_effect=inputs),
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        ):
            result = _run_keyed_picker(options, prompt="Wybierz", instruction="", fallback_invalid="INVALID")
        assert result == "a"
        assert any("INVALID" in line for line in printed)


_PLAYER = PlayerEntry(online_id="p1", player="CDA", lang_audio="jp", lang_subs="pl", max_res="1080p")


@pytest.mark.unit
class TestSourceHost:
    def test_url_shortened_to_registrable_domain(self):
        assert _source_host("http://feeds.feedburner.com/crunchyroll/rss/anime?format=xml") == "feedburner.com"

    def test_www_prefix_stripped(self):
        assert _source_host("https://www.miorosubs.com/") == "miorosubs.com"

    def test_two_label_host_kept(self):
        assert _source_host("https://miorosubs.com/") == "miorosubs.com"

    def test_non_url_text_returned_as_is(self):
        assert _source_host("own translation") == "own translation"

    def test_long_non_url_text_truncated(self):
        long_text = "x" * 50
        result = _source_host(long_text)
        assert len(result) <= 30
        assert result.endswith("…")

    def test_blank_returns_none(self):
        assert _source_host("   ") is None


@pytest.mark.unit
class TestOriginSimilar:
    def test_author_matching_host_is_similar(self):
        assert _origin_similar("Mioro-Subs", "miorosubs.com") is True

    def test_unrelated_author_and_host_differ(self):
        assert _origin_similar("Aniplex of America", "feedburner.com") is False

    def test_empty_author_is_not_similar(self):
        assert _origin_similar("", "miorosubs.com") is False


@pytest.mark.unit
class TestSelectPlayerOnce:
    def test_fallback_pick_returns_pick_signal(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            assert select_player_once([_PLAYER]) == ("pick", _PLAYER)

    def test_fallback_empty_enter_returns_back_signal(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert select_player_once([_PLAYER]) == ("back", None)

    def test_fallback_label_shows_resolved_host(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        sources = {"p1": PlayerSource(online_id="p1", host="kerapoxy.cc", embed_url="https://kerapoxy.cc/e/x")}
        printed: list[str] = []
        with (
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
            patch("builtins.input", return_value="1"),
        ):
            select_player_once([_PLAYER], sources=sources)
        assert any("kerapoxy.cc" in line for line in printed)


@pytest.mark.unit
class TestFormatPlayerSource:
    def test_full_info(self):
        p = PlayerEntry(
            online_id="p1", player="CDA", lang_audio="jp", lang_subs="pl",
            subs_author="Mioro-Subs", source="https://miorosubs.com/",
        )
        resolved = PlayerSource(online_id="p1", host="ebd.cda.pl", embed_url="https://ebd.cda.pl/620x395/xyz")
        title, body = format_player_source(p, resolved)
        assert "CDA" in title
        assert "Mioro-Subs" in body
        assert "https://miorosubs.com/" in body
        assert "https://ebd.cda.pl/620x395/xyz" in body

    def test_no_info_renders_empty_message(self):
        title, body = format_player_source(_PLAYER, None)
        assert "CDA" in title
        assert body  # the "no info" message, never blank


@pytest.mark.unit
class TestConfirm:
    def test_yes_returns_true(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            assert confirm("Kontynuować?") is True

    def test_no_returns_false(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            assert confirm("Kontynuować?") is False

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert confirm("Kontynuować?") is None
