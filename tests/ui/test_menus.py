"""Tests for ui/menus — only the pure/fallback paths (no InquirerPy TTY needed)."""

from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.content import CONTENT
from alt_ani_cli.models import PlayerSource
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, RelatedSeries, SeriesHit, SeriesRef
from alt_ani_cli.ui.menus import (
    _anchor_choice_window,
    _origin_similar,
    _run_keyed_picker,
    _run_simple_picker,
    _source_host,
    confirm,
    format_player_source,
    pick_related,
    select_action,
    select_episodes,
    select_existing_download_action,
    select_player_once,
    select_quality,
    select_series_from_download_history,
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


def _start_mode_options(monkeypatch, **counts) -> tuple[list[str], str | None]:
    monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
    printed: list[str] = []
    with (
        patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        patch("builtins.input", return_value="1"),
    ):
        choice = select_start_mode(**counts)
    return printed, choice


_SM_OPTS = CONTENT["menu"]["start_mode"]["options"]
_WATCH_PREFIX = _SM_OPTS["resume_watch"].split("(")[0].strip()
_DOWNLOAD_PREFIX = _SM_OPTS["resume_download"].split("(")[0].strip()


@pytest.mark.unit
class TestSelectStartMode:
    def test_search_is_first_option(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="1"):
            assert select_start_mode() == "search"

    def test_resume_watch_returns_resume_watch(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            assert select_start_mode(watch_count=3) == "resume_watch"

    def test_resume_download_returns_resume_download(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="3"):
            assert select_start_mode(watch_count=3, download_count=2) == "resume_download"

    def test_quit_without_history(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="3"):
            assert select_start_mode() == "quit"

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert select_start_mode() is None

    def test_no_history_shows_no_continue_options(self, monkeypatch):
        printed, _ = _start_mode_options(monkeypatch)
        assert len(printed) == 3
        assert not any(_WATCH_PREFIX in line or _DOWNLOAD_PREFIX in line for line in printed)

    def test_watch_only_shows_only_continue_watching(self, monkeypatch):
        printed, _ = _start_mode_options(monkeypatch, watch_count=22)
        assert any(_WATCH_PREFIX in line and "22" in line for line in printed)
        assert not any(_DOWNLOAD_PREFIX in line for line in printed)

    def test_download_only_shows_only_continue_downloading(self, monkeypatch):
        printed, _ = _start_mode_options(monkeypatch, download_count=3)
        assert any(_DOWNLOAD_PREFIX in line and "3" in line for line in printed)
        assert not any(_WATCH_PREFIX in line for line in printed)

    def test_both_histories_show_both_options(self, monkeypatch):
        printed, _ = _start_mode_options(monkeypatch, watch_count=22, download_count=3)
        assert any(_WATCH_PREFIX in line for line in printed)
        assert any(_DOWNLOAD_PREFIX in line for line in printed)
        assert len(printed) == 5


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

    def test_default_index_passed_to_checkbox(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", True)
        episodes = [EpisodeRow(number=n, title=f"Ep {n}", url=f"http://x/{n}") for n in (1, 2, 3)]
        prompt_obj = MagicMock()
        prompt_obj.execute.return_value = [2]
        prompt_obj.application.layout.find_all_windows.return_value = []
        with patch("InquirerPy.inquirer.checkbox", return_value=prompt_obj) as mock_cb:
            result = select_episodes(episodes, multi=True, default_index=2)
        assert result == [episodes[2]]
        assert mock_cb.call_args.kwargs["default"] == 2

    def test_downloaded_marker_differs_from_watched(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        episodes = [EpisodeRow(number=n, title=f"Ep {n}", url=f"http://x/{n}") for n in (1, 2, 3)]
        printed: list[str] = []
        with (
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
            patch("builtins.input", return_value="3"),
        ):
            select_episodes(episodes, watched_numbers={1.0}, downloaded_numbers={2.0})
        _ep = CONTENT["menu"]["episodes"]
        assert any(_ep["label_watched"].format(number=1, title="Ep 1") in line for line in printed)
        assert any(_ep["label_downloaded"].format(number=2, title="Ep 2") in line for line in printed)
        assert any(_ep["label_unwatched"].format(number=3, title="Ep 3") in line for line in printed)


@pytest.fixture
def pt_session():
    """Headless prompt_toolkit app session — no TTY required."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        yield


def _anchored_prompt(total: int, start: int):
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=f"Odcinek {i + 1}") for i in range(total)]
    prompt = inquirer.checkbox(
        message="x:",
        choices=choices,
        default=start,
        mandatory=False,
        raise_keyboard_interrupt=False,
    )
    _anchor_choice_window(prompt, start, total)
    return prompt


def _visible_episode_numbers(prompt) -> list[int]:
    """Render one frame of the choice window and return the episode numbers shown."""
    from prompt_toolkit.layout.containers import WritePosition
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen

    window = next(w for w in prompt.application.layout.find_all_windows() if w.content is prompt.content_control)
    rows = window.height().preferred
    # A real Application invalidates these per render cycle (keyed on render_counter);
    # without a running app the caches must be cleared by hand.
    prompt.content_control._fragment_cache.clear()
    prompt.content_control._content_cache.clear()
    screen = Screen()
    window.write_to_screen(
        screen,
        MouseHandlers(),
        WritePosition(xpos=0, ypos=0, width=60, height=rows),
        parent_style="",
        erase_bg=False,
        z_index=None,
    )
    numbers = []
    for y in range(rows):
        row = screen.data_buffer.get(y, {})
        text = "".join(row[x].char for x in sorted(row)).rstrip()
        if text:
            numbers.append(int(text.split("Odcinek ")[1]))
    return numbers


def _move_cursor(prompt, delta: int) -> None:
    prompt.content_control.selected_choice_index += delta
    _visible_episode_numbers(prompt)


@pytest.mark.unit
class TestAnchorChoiceWindow:
    def test_initial_view_shows_only_unwatched(self, pt_session):
        prompt = _anchored_prompt(total=12, start=11)
        assert _visible_episode_numbers(prompt) == [12]

    def test_cursor_up_grows_view_keeping_bottom(self, pt_session):
        prompt = _anchored_prompt(total=12, start=11)
        _move_cursor(prompt, -1)
        assert _visible_episode_numbers(prompt) == [11, 12]
        _move_cursor(prompt, -1)
        assert _visible_episode_numbers(prompt) == [10, 11, 12]
        _move_cursor(prompt, +1)
        assert _visible_episode_numbers(prompt) == [10, 11, 12]

    def test_window_slides_at_max_rows(self, pt_session):
        prompt = _anchored_prompt(total=30, start=20)
        assert _visible_episode_numbers(prompt) == list(range(21, 31))
        for _ in range(5):
            _move_cursor(prompt, -1)
        assert _visible_episode_numbers(prompt) == list(range(16, 31))
        _move_cursor(prompt, -1)
        assert _visible_episode_numbers(prompt) == list(range(15, 30))
        for _ in range(15):
            _move_cursor(prompt, +1)
        assert _visible_episode_numbers(prompt) == list(range(16, 31))

    def test_missing_window_is_silent_noop(self):
        prompt = MagicMock()
        prompt.application.layout.find_all_windows.return_value = []
        _anchor_choice_window(prompt, 5, 10)


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
class TestSelectSeriesFromDownloadHistory:
    _REF = SeriesRef(id="1", slug="tongari", title="Tongari Boushi no Atelier", url="https://shinden.pl/series/1-tongari")

    def test_label_shows_downloaded_count_not_last_ep(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        entries = [(self._REF, frozenset({1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0}))]
        printed: list[str] = []
        with (
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
            patch("builtins.input", return_value="1"),
        ):
            result = select_series_from_download_history(entries)
        assert result == entries[0]
        expected = CONTENT["menu"]["download_resume"]["label"].format(title=self._REF.title, count=7)
        assert any(expected in line for line in printed)
        assert not any("ostatni ep" in line for line in printed)

    def test_empty_enter_returns_none(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value=""):
            assert select_series_from_download_history([(self._REF, frozenset({1.0}))]) is None


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


_EF = CONTENT["menu"]["existing_file"]
_EXISTING = r"C:\dl\Show - ep4.mp4"


def _existing_prompt(answer: str, *, has_remaining: bool) -> tuple[str, list[str], list[str]]:
    printed: list[str] = []
    with (
        patch("builtins.input", return_value=answer),
        patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        patch("alt_ani_cli.ui.progress.warn") as warn,
    ):
        choice = select_existing_download_action(_EXISTING, has_remaining=has_remaining)
    return choice, printed, [c.args[0] for c in warn.call_args_list]


@pytest.mark.unit
class TestSelectExistingDownloadAction:
    @pytest.fixture(autouse=True)
    def _fallback(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)

    def test_single_episode_offers_skip_overwrite_cancel(self):
        _, printed, _ = _existing_prompt("1", has_remaining=False)
        opts = _EF["options"]
        assert printed == [f"  1. {opts['skip']}", f"  2. {opts['overwrite']}", f"  3. {opts['cancel']}"]

    def test_batch_with_remaining_adds_overwrite_remaining(self):
        _, printed, _ = _existing_prompt("1", has_remaining=True)
        opts = _EF["options"]
        assert printed == [
            f"  1. {opts['skip']}",
            f"  2. {opts['overwrite']}",
            f"  3. {opts['overwrite_remaining']}",
            f"  4. {opts['cancel']}",
        ]

    @pytest.mark.parametrize(
        ("answer", "expected"), [("1", "skip"), ("2", "overwrite"), ("3", "overwrite_remaining"), ("4", "cancel")]
    )
    def test_batch_fallback_maps_every_option(self, answer, expected):
        assert _existing_prompt(answer, has_remaining=True)[0] == expected

    @pytest.mark.parametrize(("answer", "expected"), [("1", "skip"), ("2", "overwrite"), ("3", "cancel")])
    def test_single_fallback_maps_every_option(self, answer, expected):
        assert _existing_prompt(answer, has_remaining=False)[0] == expected

    @pytest.mark.parametrize("has_remaining", [False, True])
    def test_empty_enter_or_esc_means_cancel(self, has_remaining):
        assert _existing_prompt("", has_remaining=has_remaining)[0] == "cancel"

    def test_header_shows_exact_path(self):
        _, _, warned = _existing_prompt("1", has_remaining=False)
        assert warned == [_EF["header"].format(path=_EXISTING)]

    def test_inquirer_esc_means_cancel(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", True)
        with (
            patch("InquirerPy.inquirer.select", return_value=MagicMock()),
            patch("alt_ani_cli.ui.menus._ask", return_value=None),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            assert select_existing_download_action(_EXISTING, has_remaining=True) == "cancel"
