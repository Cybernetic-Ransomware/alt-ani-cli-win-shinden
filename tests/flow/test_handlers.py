"""Tests for FSM navigation — ESC = go back.

Unit tests for individual handlers and integration tests via _run_interactive.
All external I/O (menus, shinden API, history) is mocked.
"""

import argparse
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from alt_ani_cli.content import CONTENT
from alt_ani_cli.errors import AntiBotError, DownloadFailedError, DownloadTargetError, NoStreamError, ShindenError
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.flow.handlers import (
    HANDLERS,
    _prefetch_player_sources,
    _prefetch_series_metadata,
    _safe_fetch_one,
    _sorted_by_date_desc,
    handle_run_action,
)
from alt_ani_cli.flow.pin import player_fingerprint
from alt_ani_cli.flow.state import BACK, FlowState, Screen, _BackSentinel
from alt_ani_cli.models import EmbedURL, PlayerSource, RelatedSeries, SeriesMetadata
from alt_ani_cli.player.runner import PlayResult
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, SeriesHit, SeriesRef


def _make_args(**overrides):
    defaults = dict(
        query=[],
        url=None,
        resume=False,
        download=False,
        delete_history=False,
        episode=None,
        quality=None,
        select_nth=None,
        vlc=False,
        no_detach=False,
        debug=False,
        player_name=None,
        lang=None,
        subs=None,
        allow_fallback=False,
        show_sources=False,
        cookies_file=None,
        cookies_browser=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_state(**overrides) -> FlowState:
    state = FlowState(args=_make_args(), client=MagicMock())
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


_SERIES_REF = SeriesRef(id="1", slug="fate", title="Fate", url="http://shinden.pl/series/1-fate")
_SERIES_HIT = SeriesHit(id="1", slug="fate", title="Fate", url="http://shinden.pl/series/1-fate")
_EP1 = EpisodeRow(number=1.0, title="Ep 1", url="http://shinden.pl/ep/1")
_EP2 = EpisodeRow(number=2.0, title="Ep 2", url="http://shinden.pl/ep/2")
_EP3 = EpisodeRow(number=3.0, title="Ep 3", url="http://shinden.pl/ep/3")
_PLAYER = PlayerEntry(online_id="pid1", player="Sibnet", lang_audio="jp", lang_subs="pl")
_PLAYER2 = PlayerEntry(online_id="pid2", player="CDA", lang_audio="jp", lang_subs="pl")


@pytest.mark.unit
class TestHandleStartMode:
    def test_esc_returns_back(self):
        state = _make_state()
        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None),
            patch("alt_ani_cli.history.list_all", return_value=[]),
        ):
            result = HANDLERS[Screen.START_MODE](state)
        assert isinstance(result, _BackSentinel)

    def test_quit_returns_none(self):
        state = _make_state()
        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value="quit"),
            patch("alt_ani_cli.history.list_all", return_value=[]),
        ):
            result = HANDLERS[Screen.START_MODE](state)
        assert result is None

    def test_search_returns_search_query(self):
        state = _make_state()
        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value="search"),
            patch("alt_ani_cli.history.list_all", return_value=[]),
        ):
            result = HANDLERS[Screen.START_MODE](state)
        assert result is Screen.SEARCH_QUERY

    def test_args_resume_skips_menu(self):
        state = _make_state(args=_make_args(resume=True))
        result = HANDLERS[Screen.START_MODE](state)
        assert result is Screen.RESUME_PICK

    def test_args_url_skips_menu_and_sets_ref(self):
        state = _make_state(args=_make_args(url="http://shinden.pl/series/1-fate"))
        with patch("alt_ani_cli.shinden.series.parse_series_url", return_value=_SERIES_REF):
            result = HANDLERS[Screen.START_MODE](state)
        assert result is Screen.FETCH_EPISODES
        assert state.ref is not None

    def test_args_query_skips_menu(self):
        state = _make_state(args=_make_args(query=["fate", "strange"]))
        result = HANDLERS[Screen.START_MODE](state)
        assert result is Screen.SERIES_PICK
        assert state.query == "fate strange"

    def test_version_signal_shows_modal_and_rerenders(self):
        from alt_ani_cli import __version__

        state = _make_state()
        signals = iter(["version", None])
        with (
            patch("alt_ani_cli.history.list_all", return_value=[]),
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=lambda **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.START_MODE](state)
        mock_modal.assert_called_once()
        assert __version__ in mock_modal.call_args[0][1]
        assert isinstance(result, _BackSentinel)


@pytest.mark.unit
class TestHandleSearchQuery:
    def test_esc_returns_back(self):
        state = _make_state()
        with patch("alt_ani_cli.ui.menus.prompt_search_query", return_value=None):
            result = HANDLERS[Screen.SEARCH_QUERY](state)
        assert isinstance(result, _BackSentinel)

    def test_query_set_and_returns_series_pick(self):
        state = _make_state()
        with patch("alt_ani_cli.ui.menus.prompt_search_query", return_value="fate"):
            result = HANDLERS[Screen.SEARCH_QUERY](state)
        assert result is Screen.SERIES_PICK
        assert state.query == "fate"


@pytest.mark.unit
class TestHandleSeriesPick:
    def test_esc_returns_back(self):
        state = _make_state(query="fate")
        with (
            patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]),
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={}),
            patch("alt_ani_cli.ui.menus.select_series_once", return_value=("back", None)),
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)
        assert isinstance(result, _BackSentinel)

    def test_pick_sets_ref_and_returns_fetch(self):
        state = _make_state(query="fate")
        with (
            patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]),
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={}),
            patch("alt_ani_cli.ui.menus.select_series_once", return_value=("pick", _SERIES_HIT)),
            patch("alt_ani_cli.shinden.series.parse_series_url", return_value=_SERIES_REF),
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)
        assert result is Screen.FETCH_EPISODES
        assert state.ref is not None

    def test_sort_signal_toggles_order(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_a = SeriesMetadata(air_date="01.01.2018", air_date_sort=(2018, 1, 1), description="", tags=(), related=())
        meta_b = SeriesMetadata(air_date="01.01.2022", air_date_sort=(2022, 1, 1), description="", tags=(), related=())

        received_hits: list[list] = []
        signals = iter([("sort", 0), ("pick", hit_b)])

        def _once(hits, metadata=None, **kw):
            received_hits.append(list(hits))
            return next(signals)

        state = _make_state(query="fate")
        with (
            patch("alt_ani_cli.shinden.search.search_series", return_value=[hit_a, hit_b]),
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={"1": meta_a, "2": meta_b}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=_once),
            patch("alt_ani_cli.shinden.series.parse_series_url", return_value=_SERIES_REF),
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        assert result is Screen.FETCH_EPISODES
        assert len(received_hits) == 2
        # After sort, newer series (2022) should come first
        assert received_hits[1][0].id == "2"
        assert received_hits[1][1].id == "1"

    def test_desc_signal_calls_show_modal_text(self):
        meta = SeriesMetadata(air_date=None, air_date_sort=None, description="Great show", tags=(), related=())
        signals = iter([("desc", 0), ("back", None)])

        state = _make_state(query="fate")
        with (
            patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]),
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={_SERIES_HIT.id: meta}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=lambda *a, **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        mock_modal.assert_called_once()
        assert isinstance(result, _BackSentinel)

    def test_tags_signal_calls_show_modal_text(self):
        meta = SeriesMetadata(air_date=None, air_date_sort=None, description="", tags=("Action", "Comedy"), related=())
        signals = iter([("tags", 0), ("back", None)])

        state = _make_state(query="fate")
        with (
            patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]),
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={_SERIES_HIT.id: meta}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=lambda *a, **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        mock_modal.assert_called_once()
        assert isinstance(result, _BackSentinel)

    def test_related_signal_substitutes_hit(self):
        related = RelatedSeries(
            id="99", slug="zero", title="Fate/Zero", url="http://shinden.pl/series/99-zero", relation="Prequel"
        )
        meta = SeriesMetadata(air_date=None, air_date_sort=None, description="", tags=(), related=(related,))

        received_hits: list[list] = []
        signals = iter([("related", 0), ("back", None)])

        def _once(hits, metadata=None, **kw):
            received_hits.append(list(hits))
            return next(signals)

        state = _make_state(query="fate")
        with (
            patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]),
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={_SERIES_HIT.id: meta}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=_once),
            patch("alt_ani_cli.ui.menus.pick_related", return_value=related),
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        assert isinstance(result, _BackSentinel)
        assert len(received_hits) == 2
        # After substitution the second call receives the new hit at position 0
        assert received_hits[1][0].id == "99"


@pytest.mark.unit
class TestHandleFetchEpisodes:
    """Single centralized diagnostics point for series_selected — covers search/url/resume alike."""

    def test_logs_series_selected_with_finalized_ref(self):
        state = _make_state(ref=_SERIES_REF, last_ep=0.0)
        with (
            patch("alt_ani_cli.shinden.series.list_episodes", return_value=(_SERIES_REF, [_EP1])),
            patch("alt_ani_cli.diagnostics.series_selected") as mock_diag,
        ):
            result = HANDLERS[Screen.FETCH_EPISODES](state)

        assert result is Screen.EPISODES_PICK
        mock_diag.assert_called_once_with(_SERIES_REF.id, _SERIES_REF.title)


@pytest.mark.unit
class TestHandleEpisodesPick:
    def test_esc_returns_back(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP1, _EP2])
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=None):
            result = HANDLERS[Screen.EPISODES_PICK](state)
        assert isinstance(result, _BackSentinel)

    def test_pick_sets_targets(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP1, _EP2])
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP1]):
            result = HANDLERS[Screen.EPISODES_PICK](state)
        assert result is Screen.EPISODE_DISPATCH
        assert state.targets == [_EP1]
        assert state.ep_idx == 0

    def test_watched_numbers_passed_to_menu(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP1, _EP2], completed_eps={1.0})
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        _, kwargs = mock_sel.call_args
        assert kwargs.get("watched_numbers") == {1.0}

    def test_resume_passes_full_list_with_cursor_on_first_unwatched(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP1, _EP2, _EP3], last_ep=2.0)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP3]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        args, kwargs = mock_sel.call_args
        assert args[0] == [_EP1, _EP2, _EP3]
        assert kwargs.get("default_index") == 2
        assert kwargs.get("watched_numbers") == {1.0, 2.0}

    def test_resume_all_watched_puts_cursor_on_last_episode(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP1, _EP2, _EP3], last_ep=3.0)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP3]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        args, kwargs = mock_sel.call_args
        assert args[0] == [_EP1, _EP2, _EP3]
        assert kwargs.get("default_index") == 2
        assert kwargs.get("watched_numbers") == {1.0, 2.0, 3.0}

    def test_no_resume_has_no_default_index(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP1, _EP2], completed_eps={1.0})
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        _, kwargs = mock_sel.call_args
        assert kwargs.get("default_index") is None
        assert kwargs.get("watched_numbers") == {1.0}

    def test_cli_episode_arg_skips_menu(self):
        state = _make_state(
            args=_make_args(episode="1"),
            ref=_SERIES_REF,
            episodes=[_EP1, _EP2],
        )
        with patch("alt_ani_cli.ui.menus.select_episodes") as mock_sel:
            result = HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_not_called()
        assert result is Screen.EPISODE_DISPATCH
        assert state.targets == [_EP1]


@pytest.mark.unit
class TestHandlePlayerPick:
    def test_esc_returns_episodes_pick(self):
        state = _make_state(ref=_SERIES_REF, targets=[_EP1], ep_idx=0, players=[_PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.EPISODES_PICK

    def test_pick_returns_resolve_stream(self):
        state = _make_state(ref=_SERIES_REF, targets=[_EP1], ep_idx=0, players=[_PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", _PLAYER)):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _PLAYER

    def test_source_signal_shows_modal_and_rerenders(self):
        state = _make_state(ref=_SERIES_REF, targets=[_EP1], ep_idx=0, players=[_PLAYER])
        signals = iter([("source", 0), ("back", None)])
        with (
            patch("alt_ani_cli.ui.menus.select_player_once", side_effect=lambda *a, **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        mock_modal.assert_called_once()
        assert result is Screen.EPISODES_PICK


@pytest.mark.unit
class TestHandleQualityPick:
    def test_esc_returns_player_pick_and_resets_quality(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {"1080p": "url"}
        state = _make_state(stream=mock_stream, quality="1080p")
        with patch("alt_ani_cli.ui.menus.select_quality", return_value=None):
            result = HANDLERS[Screen.QUALITY_PICK](state)
        assert result is Screen.PLAYER_PICK
        assert state.quality is None

    def test_pick_caches_quality(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {"720p": "url"}
        state = _make_state(stream=mock_stream)
        with patch("alt_ani_cli.ui.menus.select_quality", return_value="720p"):
            result = HANDLERS[Screen.QUALITY_PICK](state)
        assert result is Screen.ACTION_PICK
        assert state.quality == "720p"


@pytest.mark.unit
class TestHandleActionPick:
    def test_esc_resets_action_and_returns_player_pick(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {}
        state = _make_state(stream=mock_stream)
        with patch("alt_ani_cli.ui.menus.select_action", return_value=None):
            result = HANDLERS[Screen.ACTION_PICK](state)
        assert result is Screen.PLAYER_PICK
        assert state.episode_action is None

    def test_esc_returns_quality_pick_when_qualities_present(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {"720p": "url"}
        state = _make_state(stream=mock_stream)
        with patch("alt_ani_cli.ui.menus.select_action", return_value=None):
            result = HANDLERS[Screen.ACTION_PICK](state)
        assert result is Screen.QUALITY_PICK

    def test_cached_action_skips_menu(self):
        state = _make_state(episode_action="play")
        with patch("alt_ani_cli.ui.menus.select_action") as mock_act:
            result = HANDLERS[Screen.ACTION_PICK](state)
        mock_act.assert_not_called()
        assert result is Screen.RUN_ACTION

    def test_args_download_skips_menu(self):
        state = _make_state(args=_make_args(download=True))
        with patch("alt_ani_cli.ui.menus.select_action") as mock_act:
            result = HANDLERS[Screen.ACTION_PICK](state)
        mock_act.assert_not_called()
        assert result is Screen.RUN_ACTION
        assert state.episode_action == "download"


def _run_interactive_wrapped(args, client):
    """Thin wrapper to call _run_interactive with sys.exit suppressed."""
    from alt_ani_cli.cli import _run_interactive

    _run_interactive(args, client)


@pytest.mark.unit
class TestInteractiveFlow:
    def test_esc_from_start_exits_silently(self):
        """ESC at START_MODE (empty history_stack) → loop ends, no sys.exit."""
        args = _make_args()
        client = MagicMock()
        with (
            patch("alt_ani_cli.history.list_all", return_value=[]),
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None),
        ):
            _run_interactive_wrapped(args, client)

    def test_esc_from_search_query_returns_to_start(self):
        """flow: START_MODE→search→SEARCH_QUERY→ESC→START_MODE (called twice)."""
        args = _make_args()
        client = MagicMock()
        call_count = {"n": 0}

        def _fake_start(**kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "search"
            return None

        with (
            patch("alt_ani_cli.history.list_all", return_value=[]),
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=_fake_start),
            patch("alt_ani_cli.ui.menus.prompt_search_query", return_value=None),
        ):
            _run_interactive_wrapped(args, client)

        assert call_count["n"] == 2

    def test_esc_from_series_pick_returns_to_search_query(self):
        """flow: START→search→SEARCH_QUERY(fate)→SERIES_PICK→ESC→SEARCH_QUERY(ESC)→START(ESC)→exit."""
        args = _make_args()
        client = MagicMock()
        query_calls = {"n": 0}
        start_calls = {"n": 0}

        def _fake_start(**kw):
            start_calls["n"] += 1
            if start_calls["n"] == 1:
                return "search"
            return None

        def _fake_search_query():
            query_calls["n"] += 1
            if query_calls["n"] == 1:
                return "fate"
            return None

        with (
            patch("alt_ani_cli.history.list_all", return_value=[]),
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=_fake_start),
            patch("alt_ani_cli.ui.menus.prompt_search_query", side_effect=_fake_search_query),
            patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]),
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={}),
            patch("alt_ani_cli.ui.menus.select_series_once", return_value=("back", None)),
        ):
            _run_interactive_wrapped(args, client)

        assert query_calls["n"] == 2
        assert start_calls["n"] == 2

    def test_completed_eps_preserved_after_back(self):
        """After ESC from PLAYER_PICK on ep2, completed_eps has ep1 and ep_idx stays at 1.

        Simulates the per-episode handler loop directly to avoid infinite-loop
        risk from full FSM integration.
        """
        state = _make_state(
            ref=_SERIES_REF,
            episodes=[_EP1, _EP2],
            targets=[_EP1, _EP2],
            ep_idx=0,
            completed_eps=set(),
        )
        mock_ep_resp = MagicMock()
        mock_ep_resp.raise_for_status = MagicMock()
        mock_ep_resp.text = ""
        state.client.get.return_value = mock_ep_resp

        mock_stream = MagicMock()
        mock_stream.qualities = {}
        mock_embed = MagicMock()
        mock_embed.url = "https://filemoon.sx/e/abc"

        player_calls: list[int] = []

        def _fake_player(players, prompt="", failed=None, sources=None):
            player_calls.append(1)
            return ("pick", _PLAYER) if len(player_calls) == 1 else ("back", None)

        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.cli._resolve_with_fallback", return_value=(mock_stream, mock_embed)),
            patch("alt_ani_cli.ui.menus.select_player_once", side_effect=_fake_player),
            patch("alt_ani_cli.ui.menus.select_action", return_value="play"),
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert"),
        ):
            screen = Screen.EPISODE_DISPATCH
            for _ in range(30):
                result = HANDLERS[screen](state)
                if result is Screen.EPISODES_PICK:
                    final_screen = Screen.EPISODES_PICK
                    break
                screen = result
            else:
                pytest.fail("Handler loop did not return to EPISODES_PICK")

        assert final_screen is Screen.EPISODES_PICK
        assert 1.0 in state.completed_eps
        assert state.ep_idx == 1


@pytest.mark.unit
class TestHandleRunActionPlaybackReporting:
    """no_detach's return code must actually be surfaced — see mpv.py --no-detach diagnostics."""

    def _play_state(self, episode_action="play", **overrides):
        args_overrides = {"vlc": False, "download": False, "debug": False}
        args_overrides.update(overrides.pop("args_overrides", {}))
        state = _make_state(
            args=_make_args(**args_overrides),
            ref=_SERIES_REF,
            targets=[_EP1],
            ep_idx=0,
            stream=Stream(url="https://cdn.example.com/v.m3u8", headers={}, ext="m3u8"),
            embed=EmbedURL(url="https://morencius.com/embed/abc", referer="https://shinden.pl/"),
            episode_action=episode_action,
            players=[_PLAYER],
            chosen_player=_PLAYER,
            **overrides,
        )
        return state

    def test_no_detach_nonzero_rc_reports_error_not_success(self):
        state = self._play_state(args_overrides={"no_detach": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_error.assert_called_once()
        mock_success.assert_not_called()
        mock_warn.assert_not_called()

    def test_no_detach_zero_rc_slow_exit_reports_success(self):
        state = self._play_state(args_overrides={"no_detach": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_success.assert_called_once()
        mock_error.assert_not_called()
        mock_warn.assert_not_called()

    def test_no_detach_zero_rc_fast_exit_reports_unconfirmed_not_success(self):
        state = self._play_state(args_overrides={"no_detach": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.1)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_warn.assert_called_once()
        mock_success.assert_not_called()
        mock_error.assert_not_called()

    def test_detached_mode_ignores_rc_and_elapsed_reports_success(self):
        state = self._play_state(args_overrides={"no_detach": False})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=0.0)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_success.assert_called_once()
        mock_error.assert_not_called()
        mock_warn.assert_not_called()

    def test_no_detach_mpv_reports_log_file_hint(self):
        state = self._play_state(args_overrides={"no_detach": True, "vlc": False})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info") as mock_info,
        ):
            handle_run_action(state)
        mock_info.assert_called_once()
        assert "mpv-debug.log" in mock_info.call_args[0][0]

    def test_no_detach_vlc_does_not_report_mpv_log_hint(self):
        state = self._play_state(args_overrides={"no_detach": True, "vlc": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info") as mock_info,
        ):
            handle_run_action(state)
        mock_info.assert_not_called()

    def test_detached_mode_also_reports_log_file_hint(self):
        state = self._play_state(args_overrides={"no_detach": False, "vlc": False})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.0)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info") as mock_info,
        ):
            handle_run_action(state)
        mock_info.assert_called_once()
        assert "mpv-debug.log" in mock_info.call_args[0][0]


@pytest.mark.unit
class TestHandleRunActionHistoryTracking:
    """History/completed_eps must only record playback actually confirmed as watched."""

    def _play_state(self, episode_action="play", **overrides):
        args_overrides = {"vlc": False, "download": False, "debug": False}
        args_overrides.update(overrides.pop("args_overrides", {}))
        state = _make_state(
            args=_make_args(**args_overrides),
            ref=_SERIES_REF,
            targets=[_EP1],
            ep_idx=0,
            stream=Stream(url="https://cdn.example.com/v.m3u8", headers={}, ext="m3u8"),
            embed=EmbedURL(url="https://morencius.com/embed/abc", referer="https://shinden.pl/"),
            episode_action=episode_action,
            players=[_PLAYER],
            chosen_player=_PLAYER,
            **overrides,
        )
        return state

    def test_confirmed_playback_updates_history_and_completed_eps(self):
        state = self._play_state(args_overrides={"no_detach": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        mock_upsert.assert_called_once_with(_SERIES_REF, last_ep=_EP1.number)
        assert _EP1.number in state.completed_eps

    def test_unconfirmed_fast_exit_does_not_update_history(self):
        state = self._play_state(args_overrides={"no_detach": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.1)),
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.ui.progress.warn"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        mock_upsert.assert_not_called()
        assert state.completed_eps == set()

    def test_failed_playback_does_not_update_history(self):
        state = self._play_state(args_overrides={"no_detach": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        mock_upsert.assert_not_called()
        assert state.completed_eps == set()

    def test_detached_mode_updates_history_unconditionally(self):
        state = self._play_state(args_overrides={"no_detach": False})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.0)),
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.ui.progress.success"),
        ):
            handle_run_action(state)
        mock_upsert.assert_called_once_with(_SERIES_REF, last_ep=_EP1.number)
        assert _EP1.number in state.completed_eps

    def test_debug_action_does_not_update_history(self):
        # Showing the debug link table is not the same as watching the episode.
        state = self._play_state(episode_action="debug")
        with (
            patch("alt_ani_cli.cli._print_debug"),
            patch("alt_ani_cli.player.runner.play") as mock_play,
            patch("alt_ani_cli.history.upsert") as mock_upsert,
        ):
            handle_run_action(state)
        mock_play.assert_not_called()
        mock_upsert.assert_not_called()
        assert state.completed_eps == set()

    def test_download_action_does_not_update_history(self):
        state = self._play_state(episode_action="download")
        with (
            patch("alt_ani_cli.download.run") as mock_download,
            patch("alt_ani_cli.history.upsert") as mock_upsert,
        ):
            handle_run_action(state)
        mock_download.assert_called_once()
        mock_upsert.assert_not_called()
        assert state.completed_eps == set()

    def test_ep_idx_advances_regardless_of_confirmation(self):
        state = self._play_state(args_overrides={"no_detach": True})
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=5.0)),
            patch("alt_ani_cli.history.upsert"),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        assert state.ep_idx == 1


_STREAM = Stream(url="https://cdn.example.com/v.m3u8", headers={}, ext="m3u8")
_EMBED = EmbedURL(url="https://vidara.to/e/abc", referer="https://shinden.pl/")


def _download_state(players=(_PLAYER, _PLAYER2), **overrides) -> FlowState:
    state = _make_state(
        ref=_SERIES_REF,
        targets=[_EP1, _EP2],
        ep_idx=0,
        players=list(players),
        chosen_player=players[0],
        stream=_STREAM,
        embed=_EMBED,
        episode_action="download",
    )
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


@pytest.mark.unit
class TestHandleRunActionDownloadFailure:
    def test_success_advances_episode_without_history(self):
        state = _download_state()
        with (
            patch("alt_ani_cli.download.run") as mock_download,
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.diagnostics.download_result") as mock_diag,
        ):
            result = handle_run_action(state)
        mock_download.assert_called_once()
        mock_upsert.assert_not_called()
        mock_diag.assert_called_once_with(None, ok=True, exc=None)
        assert result is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1
        assert state.failed_ids == set()

    def test_failure_with_other_players_returns_to_player_pick(self):
        state = _download_state()
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
        ):
            result = handle_run_action(state)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0
        assert state.failed_ids == {_PLAYER.online_id}
        assert state.stream is None
        assert state.embed is None
        assert state.completed_eps == set()
        assert state.episode_action == "download"
        mock_upsert.assert_not_called()
        assert _PLAYER.player in mock_error.call_args.args[0]

    def test_failure_logs_redacted_diagnostics(self):
        state = _download_state()
        state.player_sources = {_PLAYER.online_id: PlayerSource(_PLAYER.online_id, "vidara.to", _EMBED.url)}
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.diagnostics.download_result") as mock_diag,
        ):
            handle_run_action(state)
        mock_diag.assert_called_once_with("vidara.to", ok=False, exc="DownloadFailedError")

    def test_failure_on_last_remaining_player_skips_episode(self):
        state = _download_state(failed_ids={_PLAYER.online_id}, chosen_player=_PLAYER2)
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = handle_run_action(state)
        assert result is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1
        assert state.completed_eps == set()
        mock_upsert.assert_not_called()
        assert mock_warn.call_args.args[0] == CONTENT["progress"]["no_player_worked"].format(number=_EP1.number)

    def test_failure_with_single_player_skips_episode(self):
        state = _download_state(players=(_PLAYER,))
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            result = handle_run_action(state)
        assert result is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1

    def test_args_download_failure_also_returns_to_player_pick(self):
        state = _download_state(episode_action=None, args=_make_args(download=True))
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            result = handle_run_action(state)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0


@pytest.mark.unit
class TestDownloadRetryOnAnotherPlayer:
    """Episode → player A → download failure → PLAYER_PICK → player B → download success → next episode."""

    def _drive(self, state: FlowState, start: Screen) -> list[Screen]:
        screens = [start]
        screen = start
        while not (screen is Screen.EPISODE_DISPATCH and state.ep_idx == 1):
            screen = HANDLERS[screen](state)
            screens.append(screen)
            assert len(screens) < 20, screens
        return screens

    def test_failed_download_retries_on_picked_player_without_asking_action(self):
        state = _download_state(stream=None, embed=None)
        stream_b = Stream(url="https://cdn.example.com/b.mp4", ext="mp4")
        streams = {_PLAYER.online_id: _STREAM, _PLAYER2.online_id: stream_b}
        downloaded: list[str] = []

        def fake_resolve(client, players, chosen, **kwargs):
            return streams[chosen.online_id], _EMBED

        def fake_download(stream, ep, ref, **kwargs):
            downloaded.append(stream.url)
            if stream is _STREAM:
                raise DownloadFailedError("x")
            return True

        with (
            patch("alt_ani_cli.cli._resolve_with_fallback", side_effect=fake_resolve),
            patch("alt_ani_cli.download.run", side_effect=fake_download),
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", _PLAYER2)) as mock_pick,
            patch("alt_ani_cli.ui.menus.select_action") as mock_action,
            patch("alt_ani_cli.history.upsert") as mock_upsert,
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            screens = self._drive(state, Screen.RESOLVE_STREAM)

        assert screens == [
            Screen.RESOLVE_STREAM,
            Screen.ACTION_PICK,
            Screen.RUN_ACTION,
            Screen.PLAYER_PICK,
            Screen.RESOLVE_STREAM,
            Screen.ACTION_PICK,
            Screen.RUN_ACTION,
            Screen.EPISODE_DISPATCH,
        ]
        assert downloaded == [_STREAM.url, stream_b.url]
        assert mock_pick.call_args.kwargs["failed"] == {_PLAYER.online_id}
        mock_action.assert_not_called()
        mock_upsert.assert_not_called()
        assert state.episode_action == "download"


@pytest.mark.unit
class TestSortedByDateDesc:
    def test_sorts_by_year_descending(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_a = SeriesMetadata(air_date="2018", air_date_sort=(2018, 1, 1), description="", tags=(), related=())
        meta_b = SeriesMetadata(air_date="2022", air_date_sort=(2022, 6, 15), description="", tags=(), related=())

        result = _sorted_by_date_desc([hit_a, hit_b], {"1": meta_a, "2": meta_b})
        assert [h.id for h in result] == ["2", "1"]

    def test_no_air_date_sorts_last(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_a = SeriesMetadata(air_date=None, air_date_sort=None, description="", tags=(), related=())
        meta_b = SeriesMetadata(air_date="2022", air_date_sort=(2022, 6, 15), description="", tags=(), related=())

        result = _sorted_by_date_desc([hit_a, hit_b], {"1": meta_a, "2": meta_b})
        assert result[0].id == "2"
        assert result[-1].id == "1"

    def test_missing_metadata_sorts_last(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_b = SeriesMetadata(air_date="2020", air_date_sort=(2020, 3, 1), description="", tags=(), related=())

        result = _sorted_by_date_desc([hit_a, hit_b], {"2": meta_b})
        assert result[0].id == "2"
        assert result[-1].id == "1"

    def test_equal_dates_preserve_relative_order(self):
        hits = [SeriesHit(id=str(i), slug=f"s{i}", title=f"S{i}", url=f"http://shinden.pl/series/{i}-s{i}") for i in range(3)]
        same_meta = SeriesMetadata(air_date="2020", air_date_sort=(2020, 1, 1), description="", tags=(), related=())
        metadata = {str(i): same_meta for i in range(3)}

        result = _sorted_by_date_desc(hits, metadata)
        assert [h.id for h in result] == ["0", "1", "2"]


def _make_ep_dispatch_state(lang=None, subs=None, player_name=None, allow_fallback=False):
    """Build a FlowState ready for EPISODE_DISPATCH with two players."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = ""
    client = MagicMock()
    client.get.return_value = mock_resp
    state = FlowState(
        args=_make_args(lang=lang, subs=subs, player_name=player_name, allow_fallback=allow_fallback),
        client=client,
    )
    state.targets = [_EP1]
    state.ep_idx = 0
    return state


@pytest.mark.unit
class TestEpisodeDispatchFilterMiss:
    def test_filters_match_proceeds_without_confirm(self):
        state = _make_ep_dispatch_state(lang="jp")
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.ui.menus.confirm") as mock_confirm,
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_confirm.assert_not_called()
        assert result in (Screen.PLAYER_PICK, Screen.RESOLVE_STREAM)
        assert state.players == [_PLAYER, _PLAYER2]

    def test_filter_miss_confirm_true_uses_full_list(self):
        state = _make_ep_dispatch_state(lang="xx")
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.ui.menus.confirm", return_value=True),
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert result in (Screen.PLAYER_PICK, Screen.RESOLVE_STREAM)
        assert state.players == [_PLAYER, _PLAYER2]

    def test_filter_miss_confirm_false_returns_episodes_pick(self):
        state = _make_ep_dispatch_state(lang="xx")
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.ui.menus.confirm", return_value=False),
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert result is Screen.EPISODES_PICK

    def test_filter_miss_confirm_none_esc_returns_episodes_pick(self):
        state = _make_ep_dispatch_state(lang="xx")
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.ui.menus.confirm", return_value=None),
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert result is Screen.EPISODES_PICK

    def test_allow_fallback_skips_confirm_and_warns(self):
        state = _make_ep_dispatch_state(lang="xx", allow_fallback=True)
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.ui.menus.confirm") as mock_confirm,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_confirm.assert_not_called()
        mock_warn.assert_called_once()
        assert result in (Screen.PLAYER_PICK, Screen.RESOLVE_STREAM)


@pytest.mark.unit
class TestEpisodeDispatchSinglePlayerAutoPick:
    def test_auto_picks_only_player_and_logs_diagnostics(self):
        state = _make_ep_dispatch_state()
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER]),
            patch("alt_ani_cli.diagnostics.player_selected") as mock_diag,
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _PLAYER
        mock_diag.assert_called_once_with(_PLAYER.online_id, _PLAYER.player, None)


@contextmanager
def _noop_spinner(msg):
    yield


_EMPTY_META = SeriesMetadata(None, None, "", (), ())


@pytest.mark.unit
class TestSafeFetchOne:
    def test_returns_metadata_from_fetch(self):
        client = MagicMock()
        meta = SeriesMetadata(air_date="01.01.2020", air_date_sort=(2020, 1, 1), description="desc", tags=(), related=())
        with (
            patch("alt_ani_cli.flow.handlers.parse_series_url", return_value=_SERIES_REF),
            patch("alt_ani_cli.flow.handlers.fetch_series_metadata", return_value=meta),
        ):
            result = _safe_fetch_one(client, _SERIES_HIT)
        assert result is meta

    def test_passes_parsed_ref_to_fetch(self):
        client = MagicMock()
        with (
            patch("alt_ani_cli.flow.handlers.parse_series_url", return_value=_SERIES_REF) as mock_parse,
            patch("alt_ani_cli.flow.handlers.fetch_series_metadata", return_value=_EMPTY_META),
        ):
            _safe_fetch_one(client, _SERIES_HIT)
        mock_parse.assert_called_once_with(_SERIES_HIT.url)


@pytest.mark.unit
class TestPrefetchSeriesMetadata:
    def test_returns_metadata_for_all_hits(self):
        client = MagicMock()
        meta = SeriesMetadata(air_date="01.01.2020", air_date_sort=(2020, 1, 1), description="", tags=(), related=())
        with (
            patch("alt_ani_cli.flow.handlers._safe_fetch_one", return_value=meta),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
        ):
            result = _prefetch_series_metadata(client, [_SERIES_HIT])
        assert result == {_SERIES_HIT.id: meta}

    def test_http_error_falls_back_and_warns(self):
        client = MagicMock()
        with (
            patch("alt_ani_cli.flow.handlers._safe_fetch_one", side_effect=CurlRequestException("timeout")),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(client, [_SERIES_HIT])
        assert _SERIES_HIT.id in result
        assert result[_SERIES_HIT.id].air_date is None
        mock_warn.assert_called_once()

    def test_shinden_error_falls_back_and_warns(self):
        client = MagicMock()
        with (
            patch("alt_ani_cli.flow.handlers._safe_fetch_one", side_effect=ShindenError("age gate")),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(client, [_SERIES_HIT])
        assert _SERIES_HIT.id in result
        assert result[_SERIES_HIT.id].air_date is None
        mock_warn.assert_called_once()

    def test_unexpected_error_propagates(self):
        client = MagicMock()
        with (
            patch("alt_ani_cli.flow.handlers._safe_fetch_one", side_effect=ValueError("unexpected")),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
        ):
            with pytest.raises(ValueError, match="unexpected"):
                _prefetch_series_metadata(client, [_SERIES_HIT])

    def test_empty_hits_returns_empty_dict(self):
        client = MagicMock()
        assert _prefetch_series_metadata(client, []) == {}

    def test_all_hits_have_entry_in_result(self):
        hits = [
            SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a"),
            SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b"),
            SeriesHit(id="3", slug="c", title="C", url="http://shinden.pl/series/3-c"),
        ]
        client = MagicMock()
        with (
            patch("alt_ani_cli.flow.handlers._safe_fetch_one", return_value=_EMPTY_META),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
        ):
            result = _prefetch_series_metadata(client, hits)
        assert set(result.keys()) == {"1", "2", "3"}

    def test_spinner_is_shown(self):
        client = MagicMock()
        spinner_calls = []

        @contextmanager
        def _recording_spinner(msg):
            spinner_calls.append(msg)
            yield

        with (
            patch("alt_ani_cli.flow.handlers._safe_fetch_one", return_value=_EMPTY_META),
            patch("alt_ani_cli.ui.progress.spinner", _recording_spinner),
        ):
            _prefetch_series_metadata(client, [_SERIES_HIT])
        assert len(spinner_calls) == 1


_EMBED = EmbedURL(url="https://www.filemoon.sx/e/abc", referer="https://shinden.pl/")


@pytest.mark.unit
class TestPrefetchPlayerSources:
    def test_success_records_host_and_embed(self):
        state = _make_state(players=[_PLAYER])
        with (
            patch("alt_ani_cli.shinden.api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
        ):
            _prefetch_player_sources(state)
        assert state.player_sources[_PLAYER.online_id].host == "filemoon.sx"
        assert state.player_sources[_PLAYER.online_id].embed_url == _EMBED.url
        assert state.player_embeds[_PLAYER.online_id] is _EMBED

    def test_antibot_error_leaves_host_unknown(self):
        state = _make_state(players=[_PLAYER, _PLAYER2])

        def _resolve(client, online_id):
            if online_id == _PLAYER.online_id:
                raise AntiBotError("blocked")
            return _EMBED

        with (
            patch("alt_ani_cli.shinden.api.resolve_embed", side_effect=_resolve),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
        ):
            _prefetch_player_sources(state)
        assert _PLAYER.online_id not in state.player_sources
        assert _PLAYER2.online_id in state.player_sources

    def test_known_ids_are_skipped(self):
        state = _make_state(players=[_PLAYER])
        state.player_sources = {_PLAYER.online_id: MagicMock()}
        with patch("alt_ani_cli.shinden.api.resolve_embed") as mock_resolve:
            _prefetch_player_sources(state)
        mock_resolve.assert_not_called()

    def test_episode_dispatch_triggers_prefetch_with_flag(self):
        state = _make_ep_dispatch_state()
        state.args.show_sources = True
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.flow.handlers._prefetch_player_sources") as mock_prefetch,
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_prefetch.assert_called_once_with(state)
        assert result is Screen.PLAYER_PICK

    def test_episode_dispatch_skips_prefetch_without_flag(self):
        state = _make_ep_dispatch_state()
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.flow.handlers._prefetch_player_sources") as mock_prefetch,
        ):
            HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_prefetch.assert_not_called()


@pytest.mark.unit
class TestResolveStreamEmbedCache:
    def _state_with_cache(self):
        state = _make_state(
            ref=_SERIES_REF,
            targets=[_EP1],
            ep_idx=0,
            players=[_PLAYER],
            chosen_player=_PLAYER,
        )
        state.player_embeds = {_PLAYER.online_id: _EMBED}
        return state

    def test_cached_embed_skips_resolve(self):
        state = self._state_with_cache()
        mock_stream = MagicMock()
        mock_stream.qualities = {}
        with (
            patch("alt_ani_cli.shinden.api.resolve_embed") as mock_resolve,
            patch("alt_ani_cli.extract.resolve", return_value=mock_stream) as mock_extract,
        ):
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        mock_resolve.assert_not_called()
        assert mock_extract.call_args[0][0] == _EMBED.url
        assert result is Screen.ACTION_PICK
        assert state.stream is mock_stream

    def test_expired_cached_embed_falls_back_to_fresh_resolve(self):
        state = self._state_with_cache()
        fresh = EmbedURL(url="https://kerapoxy.cc/e/new", referer="https://shinden.pl/")
        mock_stream = MagicMock()
        mock_stream.qualities = {}
        with (
            patch("alt_ani_cli.shinden.api.resolve_embed", return_value=fresh) as mock_resolve,
            patch("alt_ani_cli.extract.resolve", side_effect=[NoStreamError("expired"), mock_stream]) as mock_extract,
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
        ):
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        mock_resolve.assert_called_once()
        assert mock_extract.call_count == 2
        assert mock_extract.call_args[0][0] == fresh.url
        assert result is Screen.ACTION_PICK
        assert state.embed is fresh


@pytest.mark.unit
class TestResolveStreamFailureRecordsHost:
    """A player whose embed resolved but whose extraction failed should still surface its host."""

    def _state_with_two_players(self):
        return _make_state(
            ref=_SERIES_REF,
            targets=[_EP1],
            ep_idx=0,
            players=[_PLAYER, _PLAYER2],
            chosen_player=_PLAYER,
        )

    def test_extraction_failure_records_host_from_resolved_embed(self):
        state = self._state_with_two_players()
        embed = EmbedURL(url="https://playmate.to/e/xyz", referer="https://shinden.pl/")
        with (
            patch("alt_ani_cli.shinden.api.resolve_embed", return_value=embed),
            patch("alt_ani_cli.extract.resolve", side_effect=NoStreamError("dead")),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert result is Screen.PLAYER_PICK
        assert _PLAYER.online_id in state.failed_ids
        assert state.player_embeds[_PLAYER.online_id] is embed
        assert state.player_sources[_PLAYER.online_id].host == "playmate.to"

    def test_antibot_error_leaves_host_unknown(self):
        state = self._state_with_two_players()
        with (
            patch("alt_ani_cli.shinden.api.resolve_embed", side_effect=AntiBotError("blocked")),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert result is Screen.PLAYER_PICK
        assert _PLAYER.online_id in state.failed_ids
        assert _PLAYER.online_id not in state.player_embeds
        assert _PLAYER.online_id not in state.player_sources


@pytest.mark.unit
class TestResolveStreamNeverReorders:
    """order() is an auto-mode, noninteractive-only concern — the interactive picker must never trigger it."""

    def test_health_order_is_never_called(self):
        state = _make_state(
            ref=_SERIES_REF,
            targets=[_EP1],
            ep_idx=0,
            players=[_PLAYER],
            chosen_player=_PLAYER,
        )
        with (
            patch("alt_ani_cli.shinden.api.resolve_embed", side_effect=AntiBotError("blocked")),
            patch("alt_ani_cli.ui.progress.spinner", _noop_spinner),
            patch("alt_ani_cli.ui.progress.warn"),
            patch("alt_ani_cli.health.ResolverHealth.order") as mock_order,
        ):
            HANDLERS[Screen.RESOLVE_STREAM](state)

        mock_order.assert_not_called()


_EP4 = EpisodeRow(number=4.0, title="Ep 4", url="http://shinden.pl/ep/4")
_EP5 = EpisodeRow(number=5.0, title="Ep 5", url="http://shinden.pl/ep/5")
_EP6 = EpisodeRow(number=6.0, title="Ep 6", url="http://shinden.pl/ep/6")


@pytest.mark.unit
class TestStartModeHistoryOptions:
    def _run(self, choice, watch=(), downloads=(), state=None):
        state = state or _make_state()
        with (
            patch("alt_ani_cli.history.list_all", return_value=list(watch)),
            patch("alt_ani_cli.history.list_downloads", return_value=list(downloads)),
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value=choice) as mock_menu,
        ):
            result = HANDLERS[Screen.START_MODE](state)
        return state, result, mock_menu.call_args.kwargs

    def test_menu_receives_both_counts(self):
        _, _, kwargs = self._run(
            None,
            watch=[(_SERIES_REF, 1.0)] * 22,
            downloads=[(_SERIES_REF, frozenset({1.0}))] * 3,
        )
        assert kwargs == {"watch_count": 22, "download_count": 3}

    def test_resume_watch_goes_to_resume_pick(self):
        _, result, _ = self._run("resume_watch", watch=[(_SERIES_REF, 1.0)])
        assert result is Screen.RESUME_PICK

    def test_resume_download_goes_to_download_resume_pick(self):
        _, result, _ = self._run("resume_download", downloads=[(_SERIES_REF, frozenset({1.0}))])
        assert result is Screen.DOWNLOAD_RESUME_PICK

    @pytest.mark.parametrize(
        "choice, expected",
        [("search", Screen.SEARCH_QUERY), ("url", Screen.URL_INPUT), ("resume_watch", Screen.RESUME_PICK)],
    )
    def test_leaving_download_resume_clears_its_state(self, choice, expected):
        state = _make_state(resume_mode="download", downloaded_eps={1.0}, episode_action="download")
        _, result, _ = self._run(choice, state=state)
        assert result is expected
        assert state.resume_mode is None
        assert state.downloaded_eps == set()
        assert state.episode_action is None

    def test_cached_download_action_outside_resume_mode_is_kept(self):
        state = _make_state(episode_action="download")
        self._run("search", state=state)
        assert state.episode_action == "download"

    def test_args_resume_with_download_skips_menu_to_download_resume(self):
        state = _make_state(args=_make_args(resume=True, download=True))
        assert HANDLERS[Screen.START_MODE](state) is Screen.DOWNLOAD_RESUME_PICK


@pytest.mark.unit
class TestHandleResumePickModes:
    def test_resume_watch_keeps_last_ep(self):
        state = _make_state(resume_mode="download", downloaded_eps={1.0}, episode_action="download")
        with (
            patch("alt_ani_cli.history.list_all", return_value=[(_SERIES_REF, 5.0)]),
            patch("alt_ani_cli.ui.menus.select_series_from_history", return_value=(_SERIES_REF, 5.0)),
        ):
            result = HANDLERS[Screen.RESUME_PICK](state)
        assert result is Screen.FETCH_EPISODES
        assert state.ref == _SERIES_REF
        assert state.last_ep == 5.0
        assert state.resume_mode == "watch"
        assert state.downloaded_eps == set()
        assert state.episode_action is None

    def test_resume_download_sets_download_state(self):
        state = _make_state(last_ep=3.0)
        with (
            patch("alt_ani_cli.history.list_downloads", return_value=[(_SERIES_REF, frozenset({1.0, 2.0}))]),
            patch(
                "alt_ani_cli.ui.menus.select_series_from_download_history",
                return_value=(_SERIES_REF, frozenset({1.0, 2.0})),
            ),
        ):
            result = HANDLERS[Screen.DOWNLOAD_RESUME_PICK](state)
        assert result is Screen.FETCH_EPISODES
        assert state.ref == _SERIES_REF
        assert state.resume_mode == "download"
        assert state.downloaded_eps == {1.0, 2.0}
        assert state.episode_action == "download"
        assert state.last_ep == 0.0

    def test_resume_download_esc_returns_back(self):
        state = _make_state()
        with (
            patch("alt_ani_cli.history.list_downloads", return_value=[(_SERIES_REF, frozenset({1.0}))]),
            patch("alt_ani_cli.ui.menus.select_series_from_download_history", return_value=None),
        ):
            result = HANDLERS[Screen.DOWNLOAD_RESUME_PICK](state)
        assert isinstance(result, _BackSentinel)
        assert state.resume_mode is None
        assert state.episode_action is None

    def test_resume_download_empty_history_returns_back(self):
        state = _make_state()
        with (
            patch("alt_ani_cli.history.list_downloads", return_value=[]),
            patch("alt_ani_cli.ui.progress.error") as mock_err,
        ):
            result = HANDLERS[Screen.DOWNLOAD_RESUME_PICK](state)
        assert isinstance(result, _BackSentinel)
        mock_err.assert_called_once()


@pytest.mark.unit
class TestEpisodesPickDownloadResume:
    def _pick(self, episodes, downloaded):
        state = _make_state(ref=_SERIES_REF, episodes=episodes, resume_mode="download", downloaded_eps=set(downloaded))
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[episodes[-1]]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        return mock_sel.call_args

    def test_passes_downloaded_numbers_and_cursor_on_first_missing(self):
        args, kwargs = self._pick([_EP1, _EP2, _EP3], {1.0, 2.0})
        assert args[0] == [_EP1, _EP2, _EP3]
        assert kwargs["downloaded_numbers"] == {1.0, 2.0}
        assert kwargs["default_index"] == 2
        assert kwargs["watched_numbers"] == set()

    def test_gap_in_downloads_puts_cursor_on_gap_not_after_max(self):
        _, kwargs = self._pick([_EP1, _EP2, _EP3, _EP4, _EP5, _EP6], {1.0, 2.0, 3.0, 5.0})
        assert kwargs["default_index"] == 3

    def test_all_downloaded_shows_full_list_with_cursor_on_last(self):
        args, kwargs = self._pick([_EP1, _EP2, _EP3], {1.0, 2.0, 3.0})
        assert args[0] == [_EP1, _EP2, _EP3]
        assert kwargs["default_index"] == 2

    def test_watch_resume_cursor_and_watched_ignore_downloads(self):
        state = _make_state(
            ref=_SERIES_REF, episodes=[_EP1, _EP2, _EP3], last_ep=1.0, resume_mode="watch", downloaded_eps={2.0}
        )
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        kwargs = mock_sel.call_args.kwargs
        assert kwargs["watched_numbers"] == {1.0}
        assert kwargs["default_index"] == 1

    def test_manual_download_outside_resume_marks_episode_without_moving_cursor(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP1, _EP2, _EP3], downloaded_eps={1.0})
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        kwargs = mock_sel.call_args.kwargs
        assert kwargs["downloaded_numbers"] == {1.0}
        assert kwargs["watched_numbers"] == set()
        assert kwargs["default_index"] is None


@pytest.mark.unit
class TestRunActionDownloadHistory:
    def test_success_records_download_and_updates_state(self):
        state = _download_state(downloaded_eps={5.0})
        with (
            patch("alt_ani_cli.download.run"),
            patch("alt_ani_cli.history.record_download") as mock_record,
            patch("alt_ani_cli.history.upsert") as mock_upsert,
        ):
            handle_run_action(state)
        mock_record.assert_called_once_with(_SERIES_REF, _EP1.number)
        mock_upsert.assert_not_called()
        assert state.downloaded_eps == {5.0, _EP1.number}
        assert state.completed_eps == set()

    def test_failure_does_not_record_download(self):
        state = _download_state()
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.history.record_download") as mock_record,
            patch("alt_ani_cli.ui.progress.error"),
        ):
            handle_run_action(state)
        mock_record.assert_not_called()
        assert state.downloaded_eps == set()

    def test_success_is_persisted_without_touching_watch_history(self):
        from alt_ani_cli import history

        state = _download_state()
        with patch("alt_ani_cli.download.run"):
            handle_run_action(state)
        assert history.list_downloads() == [(_SERIES_REF, frozenset({_EP1.number}))]
        assert history.list_all() == []


@pytest.mark.unit
class TestEpisodeDispatchDownloadSorting:
    def test_cached_download_action_uses_download_ranking(self):
        state = _make_ep_dispatch_state()
        state.episode_action = "download"
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]) as mock_sort,
        ):
            HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert mock_sort.call_args.kwargs["download"] is True

    def test_play_action_keeps_playback_ranking(self):
        state = _make_ep_dispatch_state()
        state.episode_action = "play"
        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]) as mock_sort,
        ):
            HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert mock_sort.call_args.kwargs["download"] is False


@pytest.mark.unit
class TestDownloadResumeBackToSearchRegression:
    def test_back_from_download_resume_to_search_clears_download_mode(self):
        """START→resume_download→pick→EPISODES_PICK(ESC)→DOWNLOAD_RESUME_PICK(ESC)→START→search."""
        captured: dict = {}
        seen_at_search: dict = {}
        starts = iter(["resume_download", "search", None])
        download_picks = iter([(_SERIES_REF, frozenset({1.0})), None])

        def _factory(**kw):
            captured["state"] = FlowState(**kw)
            return captured["state"]

        def _fake_search_query():
            s = captured["state"]
            seen_at_search.update(
                resume_mode=s.resume_mode, episode_action=s.episode_action, downloaded_eps=set(s.downloaded_eps)
            )
            return None

        with (
            patch("alt_ani_cli.flow.state.FlowState", side_effect=_factory),
            patch("alt_ani_cli.history.list_all", return_value=[]),
            patch("alt_ani_cli.history.list_downloads", return_value=[(_SERIES_REF, frozenset({1.0}))]),
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=lambda **kw: next(starts)),
            patch("alt_ani_cli.ui.menus.select_series_from_download_history", side_effect=lambda e: next(download_picks)),
            patch("alt_ani_cli.shinden.series.list_episodes", return_value=(_SERIES_REF, [_EP1, _EP2])),
            patch("alt_ani_cli.ui.menus.select_episodes", return_value=None),
            patch("alt_ani_cli.ui.menus.prompt_search_query", side_effect=_fake_search_query),
        ):
            _run_interactive_wrapped(_make_args(), MagicMock())

        assert seen_at_search == {"resume_mode": None, "episode_action": None, "downloaded_eps": set()}


_EP7 = EpisodeRow(number=7.0, title="Ep 7", url="http://shinden.pl/ep/7")


class _FakeDownloads:
    """Stands in for download.run: consults confirm_overwrite for 'existing' episodes like the real one does."""

    def __init__(self, existing=(), fail_urls=()):
        self.existing = set(existing)
        self.fail_urls = set(fail_urls)
        self.events: list[tuple[str, float]] = []

    def path(self, ep) -> Path:
        return Path(f"C:/dl/Fate - ep{ep.number:g}.mp4")

    def __call__(self, stream, ep, ref, *, confirm_overwrite=None):
        replacing = False
        if confirm_overwrite is not None and ep.number in self.existing:
            if not confirm_overwrite(self.path(ep)):
                self.events.append(("kept", ep.number))
                return False
            replacing = True
        if stream.url in self.fail_urls:
            self.events.append(("failed", ep.number))
            raise DownloadFailedError("x")
        self.events.append(("replaced" if replacing else "downloaded", ep.number))
        return True


def _batch_state(targets, **overrides) -> FlowState:
    return _download_state(targets=list(targets), **overrides)


def _run_download(state, fake, choice=None):
    with (
        patch("alt_ani_cli.download.run", side_effect=fake),
        patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value=choice) as mock_prompt,
        patch("alt_ani_cli.history.record_download") as mock_record,
        patch("alt_ani_cli.ui.progress.error"),
        patch("alt_ani_cli.ui.progress.warn"),
        patch("alt_ani_cli.ui.progress.info"),
    ):
        result = handle_run_action(state)
    return result, mock_prompt, mock_record


@pytest.mark.unit
class TestRunActionExistingFile:
    def test_missing_file_downloads_without_prompt(self):
        state = _batch_state([_EP4, _EP5])
        fake = _FakeDownloads()
        result, prompt, record = _run_download(state, fake)
        prompt.assert_not_called()
        assert fake.events == [("downloaded", 4.0)]
        record.assert_called_once_with(_SERIES_REF, 4.0)
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)

    def test_skip_keeps_file_and_records_download(self):
        state = _batch_state([_EP4, _EP5])
        fake = _FakeDownloads(existing={4.0})
        result, _, record = _run_download(state, fake, "skip")
        assert fake.events == [("kept", 4.0)]
        record.assert_called_once_with(_SERIES_REF, 4.0)
        assert state.downloaded_eps == {4.0}
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)
        assert state.overwrite_existing_batch is False

    def test_skip_does_not_report_download_result(self):
        state = _batch_state([_EP4])
        with patch("alt_ani_cli.diagnostics.download_result") as mock_diag:
            _run_download(state, _FakeDownloads(existing={4.0}), "skip")
        mock_diag.assert_not_called()

    def test_overwrite_replaces_and_records_without_batch_policy(self):
        state = _batch_state([_EP4, _EP5])
        fake = _FakeDownloads(existing={4.0})
        result, _, record = _run_download(state, fake, "overwrite")
        assert fake.events == [("replaced", 4.0)]
        record.assert_called_once_with(_SERIES_REF, 4.0)
        assert state.downloaded_eps == {4.0}
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)
        assert state.overwrite_existing_batch is False

    def test_cancel_returns_to_episode_pick_without_history(self):
        state = _batch_state([_EP4, _EP5, _EP6])
        fake = _FakeDownloads(existing={4.0})
        result, _, record = _run_download(state, fake, "cancel")
        assert result is Screen.EPISODES_PICK
        assert fake.events == [("kept", 4.0)]
        record.assert_not_called()
        assert state.downloaded_eps == set()
        assert state.ep_idx == 0
        assert state.overwrite_existing_batch is False
        assert state.stream is None

    def test_overwrite_remaining_sets_batch_policy(self):
        state = _batch_state([_EP4, _EP5])
        fake = _FakeDownloads(existing={4.0})
        _run_download(state, fake, "overwrite_remaining")
        assert fake.events == [("replaced", 4.0)]
        assert state.overwrite_existing_batch is True

    def test_policy_skips_prompt_for_next_existing_episode(self):
        state = _batch_state([_EP4, _EP5], ep_idx=1, overwrite_existing_batch=True)
        fake = _FakeDownloads(existing={5.0})
        _, prompt, record = _run_download(state, fake)
        prompt.assert_not_called()
        assert fake.events == [("replaced", 5.0)]
        record.assert_called_once_with(_SERIES_REF, 5.0)

    def test_policy_downloads_missing_episode_normally(self):
        state = _batch_state([_EP4, _EP5], ep_idx=1, overwrite_existing_batch=True)
        fake = _FakeDownloads()
        _, prompt, _ = _run_download(state, fake)
        prompt.assert_not_called()
        assert fake.events == [("downloaded", 5.0)]

    def test_failed_overwrite_returns_to_player_pick_and_keeps_policy(self):
        state = _batch_state([_EP4, _EP5], overwrite_existing_batch=True)
        fake = _FakeDownloads(existing={4.0}, fail_urls={_STREAM.url})
        result, _, record = _run_download(state, fake)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0
        record.assert_not_called()
        assert state.downloaded_eps == set()
        assert state.overwrite_existing_batch is True

    @pytest.mark.parametrize(
        ("targets", "ep_idx", "has_remaining"),
        [([_EP4], 0, False), ([_EP4, _EP5], 0, True), ([_EP4, _EP5], 1, False)],
    )
    def test_batch_option_offered_only_with_remaining_episodes(self, targets, ep_idx, has_remaining):
        state = _batch_state(targets, ep_idx=ep_idx)
        ep = targets[ep_idx]
        _, prompt, _ = _run_download(state, _FakeDownloads(existing={ep.number}), "skip")
        assert prompt.call_args.args == (str(_FakeDownloads().path(ep)),)
        assert prompt.call_args.kwargs == {"has_remaining": has_remaining}

    def test_already_downloaded_episode_still_prompts(self):
        state = _batch_state([_EP4], downloaded_eps={4.0})
        _, prompt, _ = _run_download(state, _FakeDownloads(existing={4.0}), "skip")
        prompt.assert_called_once()


@pytest.mark.unit
class TestOverwriteBatchScenario:
    def test_remaining_policy_survives_player_retry(self):
        """ep4 existing → overwrite remaining; ep5 missing; ep6 fails on player A, retried on B; ep7 auto."""
        state = _batch_state([_EP4, _EP5, _EP6, _EP7])
        stream_b = Stream(url="https://cdn.example.com/b.mp4", ext="mp4")
        fake = _FakeDownloads(existing={4.0, 6.0, 7.0})
        prompts: list[float] = []

        def answer(path, *, has_remaining):
            prompts.append(state.current_ep.number)
            return "overwrite_remaining"

        screens = []
        with (
            patch("alt_ani_cli.download.run", side_effect=fake),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", side_effect=answer),
            patch("alt_ani_cli.history.record_download") as mock_record,
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            for stream in (_STREAM, _STREAM):
                state.stream = stream
                screens.append(handle_run_action(state))
            fake.fail_urls = {_STREAM.url}
            state.stream = _STREAM
            screens.append(handle_run_action(state))
            state.chosen_player = _PLAYER2
            state.stream = stream_b
            screens.append(handle_run_action(state))
            fake.fail_urls = set()
            state.stream = _STREAM
            screens.append(handle_run_action(state))

        assert prompts == [4.0]
        assert fake.events == [
            ("replaced", 4.0),
            ("downloaded", 5.0),
            ("failed", 6.0),
            ("replaced", 6.0),
            ("replaced", 7.0),
        ]
        assert screens == [Screen.EPISODE_DISPATCH, Screen.EPISODE_DISPATCH, Screen.PLAYER_PICK] + [Screen.EPISODE_DISPATCH] * 2
        assert [c.args[1] for c in mock_record.call_args_list] == [4.0, 5.0, 6.0, 7.0]

    def test_skip_in_batch_continues_with_next_episode(self):
        state = _batch_state([_EP4, _EP5, _EP6])
        fake = _FakeDownloads(existing={4.0})
        with (
            patch("alt_ani_cli.download.run", side_effect=fake),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value="skip"),
            patch("alt_ani_cli.history.record_download") as mock_record,
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
            state.stream = _STREAM
            handle_run_action(state)
        assert fake.events == [("kept", 4.0), ("downloaded", 5.0)]
        assert [c.args[1] for c in mock_record.call_args_list] == [4.0, 5.0]
        assert state.ep_idx == 2


@pytest.mark.unit
class TestOverwriteBatchPolicyReset:
    def test_new_episode_selection_resets_policy(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP4, _EP5], overwrite_existing_batch=True)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP5]):
            HANDLERS[Screen.EPISODES_PICK](state)
        assert state.overwrite_existing_batch is False

    def test_cli_episode_range_batch_resets_policy(self):
        state = _make_state(
            ref=_SERIES_REF, episodes=[_EP4, _EP5], overwrite_existing_batch=True, args=_make_args(episode="4-5")
        )
        HANDLERS[Screen.EPISODES_PICK](state)
        assert state.overwrite_existing_batch is False

    def test_new_series_resets_policy(self):
        state = _make_state(ref=_SERIES_REF, overwrite_existing_batch=True)
        with patch("alt_ani_cli.shinden.series.list_episodes", return_value=(_SERIES_REF, [_EP1])):
            HANDLERS[Screen.FETCH_EPISODES](state)
        assert state.overwrite_existing_batch is False

    def test_player_pick_after_failure_keeps_policy(self):
        state = _batch_state([_EP4, _EP5], overwrite_existing_batch=True, failed_ids={_PLAYER.online_id})
        with (
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", _PLAYER2)),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            HANDLERS[Screen.PLAYER_PICK](state)
        assert state.overwrite_existing_batch is True


@pytest.mark.unit
class TestSkipBackfillsDownloadHistory:
    def test_skip_on_file_unknown_to_history_marks_it_downloaded(self):
        from alt_ani_cli import history

        state = _batch_state([_EP4])
        assert history.list_downloads() == []
        with (
            patch("alt_ani_cli.download.run", side_effect=_FakeDownloads(existing={4.0})),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value="skip"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        assert history.list_downloads() == [(_SERIES_REF, frozenset({4.0}))]

    def test_skipped_episode_is_marked_on_next_download_resume(self):
        from alt_ani_cli import history

        state = _batch_state([_EP4])
        with (
            patch("alt_ani_cli.download.run", side_effect=_FakeDownloads(existing={4.0})),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value="skip"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        resumed = _make_state()
        with patch("alt_ani_cli.ui.menus.select_series_from_download_history", side_effect=lambda e: e[0]):
            HANDLERS[Screen.DOWNLOAD_RESUME_PICK](resumed)
        assert resumed.downloaded_eps == {4.0}


@pytest.mark.unit
class TestEpisodeArgConsumedOnce:
    def _state(self, **overrides) -> FlowState:
        return _make_state(
            ref=_SERIES_REF,
            episodes=[_EP4, _EP5, _EP6, _EP7],
            args=_make_args(episode="4-7"),
            **overrides,
        )

    def test_first_entry_auto_selects_range_without_picker(self):
        state = self._state()
        with patch("alt_ani_cli.ui.menus.select_episodes") as mock_sel:
            result = HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_not_called()
        assert result is Screen.EPISODE_DISPATCH
        assert state.targets == [_EP4, _EP5, _EP6, _EP7]
        assert state.episode_arg_consumed is True

    def test_cancel_on_existing_file_then_shows_picker(self):
        state = self._state()
        HANDLERS[Screen.EPISODES_PICK](state)
        state.players = [_PLAYER, _PLAYER2]
        state.chosen_player = _PLAYER
        state.stream = _STREAM
        state.episode_action = "download"
        fake = _FakeDownloads(existing={4.0})
        result, _, record = _run_download(state, fake, "cancel")
        assert result is Screen.EPISODES_PICK
        assert state.overwrite_existing_batch is False
        record.assert_not_called()
        assert fake.events == [("kept", 4.0)]
        assert state.ep_idx == 0

        with (
            patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP5]) as mock_sel,
            patch("alt_ani_cli.cli._parse_range") as mock_parse,
        ):
            result = HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_called_once()
        mock_parse.assert_not_called()
        assert result is Screen.EPISODE_DISPATCH
        assert state.targets == [_EP5]

    def test_esc_from_player_pick_then_shows_picker(self):
        state = self._state()
        HANDLERS[Screen.EPISODES_PICK](state)
        state.players = [_PLAYER, _PLAYER2]
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)):
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.EPISODES_PICK
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=None) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_called_once()

    def test_new_series_reapplies_episode_arg(self):
        state = self._state(episode_arg_consumed=True)
        with patch("alt_ani_cli.shinden.series.list_episodes", return_value=(_SERIES_REF, [_EP4, _EP5, _EP6, _EP7])):
            HANDLERS[Screen.FETCH_EPISODES](state)
        assert state.episode_arg_consumed is False
        with patch("alt_ani_cli.ui.menus.select_episodes") as mock_sel:
            assert HANDLERS[Screen.EPISODES_PICK](state) is Screen.EPISODE_DISPATCH
        mock_sel.assert_not_called()

    def test_without_episode_arg_picker_always_shown(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP4, _EP5])
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP4]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
            HANDLERS[Screen.EPISODES_PICK](state)
        assert mock_sel.call_count == 2
        assert state.episode_arg_consumed is False


@pytest.mark.unit
class TestRunActionDownloadTargetError:
    def _run(self, state):
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadTargetError("C:/dl/Fate - ep4.mp4 locked")),
            patch("alt_ani_cli.history.record_download") as mock_record,
            patch("alt_ani_cli.diagnostics.download_result") as mock_diag,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
        ):
            result = handle_run_action(state)
        return result, mock_record, mock_diag, mock_error

    def test_local_file_error_does_not_fail_the_player(self):
        state = _batch_state([_EP4, _EP5], overwrite_existing_batch=True)
        result, record, diag, error = self._run(state)
        assert result is Screen.PLAYER_PICK
        assert state.failed_ids == set()
        assert state.ep_idx == 0
        assert state.overwrite_existing_batch is True
        assert state.stream is None
        record.assert_not_called()
        diag.assert_not_called()
        assert error.call_args.args[0] == "C:/dl/Fate - ep4.mp4 locked"

    def test_single_player_episode_is_not_skipped(self):
        state = _batch_state([_EP4, _EP5], players=(_PLAYER,))
        result, *_ = self._run(state)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0
        assert state.failed_ids == set()

    def test_player_pick_offers_same_player_again(self):
        state = _batch_state([_EP4])
        self._run(state)
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", _PLAYER)) as mock_pick:
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.RESOLVE_STREAM
        assert mock_pick.call_args.kwargs["failed"] == set()
        assert state.chosen_player is _PLAYER


_PIN_A1 = PlayerEntry(
    online_id="a1", player="CDA", lang_audio="jp", lang_subs="pl", subs_author="Mioro", source="https://www.miorosubs.com/?ep=4"
)
_PIN_A2 = PlayerEntry(
    online_id="a2", player="CDA", lang_audio="jp", lang_subs="pl", subs_author="Mioro", source="http://miorosubs.com/?ep=5"
)
_PIN_A3 = PlayerEntry(
    online_id="a3", player="CDA", lang_audio="jp", lang_subs="pl", subs_author="Mioro", source="https://miorosubs.com/x"
)
_OTHER = PlayerEntry(online_id="o1", player="Sibnet", lang_audio="jp", lang_subs="pl", source="https://anisubs.pl/")
_OTHER_EN = PlayerEntry(online_id="o2", player="CDA", lang_audio="jp", lang_subs="en")
_PIN_FP = player_fingerprint(_PIN_A1)


def _pin_batch_state(**overrides) -> FlowState:
    defaults = dict(
        targets=[_EP4, _EP5, _EP6],
        ep_idx=1,
        players=[_PIN_A1, _OTHER],
        chosen_player=_PIN_A1,
        episode_action="download",
        pinned_player=_PIN_FP,
    )
    defaults.update(overrides)
    return _download_state(**defaults)


def _pin_dispatch(players, *, state=None, confirm=True, **arg_overrides):
    """Run EPISODE_DISPATCH with parse/sort mocked; sort_players returns `players` as the final order."""
    if state is None:
        state = _pin_batch_state(args=_make_args(**arg_overrides))
    resp = MagicMock()
    resp.text = ""
    state.client.get.return_value = resp
    with (
        patch("alt_ani_cli.shinden.episode.parse_players", return_value=list(players)),
        patch("alt_ani_cli.shinden.episode.sort_players", return_value=list(players)) as mock_sort,
        patch("alt_ani_cli.shinden.api.resolve_embed") as mock_embed,
        patch("alt_ani_cli.diagnostics.player_selected") as mock_diag,
        patch("alt_ani_cli.ui.menus.confirm", return_value=confirm),
        patch("alt_ani_cli.ui.progress.info") as mock_info,
        patch("alt_ani_cli.ui.progress.warn"),
    ):
        result = HANDLERS[Screen.EPISODE_DISPATCH](state)
    return state, result, {"sort": mock_sort, "embed": mock_embed, "diag": mock_diag, "info": mock_info}


@pytest.mark.unit
class TestPinFirstSelection:
    def test_download_and_pin_sets_pin_and_downloads_current_episode(self):
        state = _pin_batch_state(ep_idx=0, episode_action=None, pinned_player=None)
        with patch("alt_ani_cli.ui.menus.select_action", return_value="download_pin") as mock_action:
            assert HANDLERS[Screen.ACTION_PICK](state) is Screen.RUN_ACTION
        assert mock_action.call_args.kwargs == {"offer_pin": True}
        assert state.episode_action == "download"
        assert state.pinned_player == _PIN_FP

        fake = _FakeDownloads()
        result, _, record = _run_download(state, fake)
        assert fake.events == [("downloaded", 4.0)]
        record.assert_called_once_with(_SERIES_REF, 4.0)
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)
        assert state.pinned_player == _PIN_FP

    @pytest.mark.parametrize(("targets", "ep_idx"), [([_EP4], 0), ([_EP4, _EP5], 1)])
    def test_option_hidden_for_last_or_only_target(self, targets, ep_idx):
        state = _pin_batch_state(targets=targets, ep_idx=ep_idx, episode_action=None, pinned_player=None)
        with patch("alt_ani_cli.ui.menus.select_action", return_value="download") as mock_action:
            HANDLERS[Screen.ACTION_PICK](state)
        assert mock_action.call_args.kwargs == {"offer_pin": False}
        assert state.pinned_player is None

    def test_plain_download_does_not_pin(self):
        state = _pin_batch_state(ep_idx=0, episode_action=None, pinned_player=None)
        with patch("alt_ani_cli.ui.menus.select_action", return_value="download"):
            HANDLERS[Screen.ACTION_PICK](state)
        assert state.pinned_player is None

    def test_pinning_does_not_touch_overwrite_policy(self):
        state = _pin_batch_state(ep_idx=0, episode_action=None, pinned_player=None)
        with patch("alt_ani_cli.ui.menus.select_action", return_value="download_pin"):
            HANDLERS[Screen.ACTION_PICK](state)
        assert state.overwrite_existing_batch is False


@pytest.mark.unit
class TestPinAutoMatch:
    def test_same_fingerprint_new_online_id_skips_player_pick(self):
        state, result, mocks = _pin_dispatch([_OTHER, _PIN_A2])
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _PIN_A2
        assert state.player_picked_manually is False
        mocks["diag"].assert_called_once_with(_PIN_A2.online_id, _PIN_A2.player, None)
        assert state.pinned_player == _PIN_FP

    def test_matching_issues_no_extra_requests(self):
        state, _, mocks = _pin_dispatch([_OTHER, _PIN_A2])
        mocks["embed"].assert_not_called()
        state.client.get.assert_called_once_with(_EP5.url)
        assert state.player_sources == {}

    def test_cached_download_action_keeps_download_sort(self):
        _, _, mocks = _pin_dispatch([_OTHER, _PIN_A2])
        assert mocks["sort"].call_args.kwargs == {"download": True}

    def test_first_of_several_identical_fingerprints_in_sorted_order(self):
        state, _, _ = _pin_dispatch([_OTHER, _PIN_A3, _PIN_A2])
        assert state.chosen_player is _PIN_A3

    def test_match_runs_on_filtered_list(self):
        state, result, _ = _pin_dispatch([_OTHER_EN, _OTHER, _PIN_A2], subs="pl")
        assert result is Screen.RESOLVE_STREAM
        assert state.players == [_OTHER, _PIN_A2]
        assert state.chosen_player is _PIN_A2

    def test_pinned_candidate_filtered_out_shows_normal_picker(self):
        state, result, _ = _pin_dispatch([_OTHER, _PIN_A2, _OTHER_EN], player_name="CDA", subs="en")
        assert result is Screen.PLAYER_PICK
        assert state.players == [_OTHER_EN]
        assert state.chosen_player is None
        assert state.pinned_player == _PIN_FP

    def test_pinned_candidate_filtered_out_of_several_shows_picker(self):
        other_sibnet = PlayerEntry(online_id="o3", player="Sibnet", lang_audio="jp", lang_subs="en")
        state, result, _ = _pin_dispatch([_OTHER, _PIN_A2, other_sibnet], player_name="Sibnet")
        assert result is Screen.PLAYER_PICK
        assert state.players == [_OTHER, other_sibnet]
        assert state.chosen_player is None
        assert state.pinned_player == _PIN_FP

    def test_filter_fallback_full_list_can_match_pin(self):
        state, result, _ = _pin_dispatch([_OTHER, _PIN_A2], lang="xx", allow_fallback=True)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _PIN_A2

    def test_no_match_shows_picker_and_keeps_pin(self):
        state, result, mocks = _pin_dispatch([_OTHER, _OTHER_EN])
        assert result is Screen.PLAYER_PICK
        assert state.chosen_player is None
        assert state.pinned_player == _PIN_FP
        mocks["info"].assert_called_with(CONTENT["progress"]["pin_no_match"].format(number=5.0))

    def test_without_pin_dispatch_is_unchanged(self):
        state, result, mocks = _pin_dispatch([_OTHER, _PIN_A2], state=_pin_batch_state(pinned_player=None))
        assert result is Screen.PLAYER_PICK
        mocks["diag"].assert_not_called()
        no_match = CONTENT["progress"]["pin_no_match"].format(number=5.0)
        assert all(c.args[0] != no_match for c in mocks["info"].call_args_list)

    def test_select_nth_takes_precedence_over_pin(self):
        state, result, mocks = _pin_dispatch([_OTHER, _PIN_A2], select_nth=1)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _OTHER
        assert state.pinned_player == _PIN_FP
        mocks["diag"].assert_called_once_with(_OTHER.online_id, _OTHER.player, None)

    def test_pin_without_match_and_single_player_shows_picker(self):
        state, result, mocks = _pin_dispatch([_OTHER])
        assert result is Screen.PLAYER_PICK
        assert state.chosen_player is None
        assert state.pinned_player == _PIN_FP
        mocks["diag"].assert_not_called()

    def test_pin_with_match_and_single_player_auto_picks_match(self):
        state, result, _ = _pin_dispatch([_PIN_A2])
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _PIN_A2

    def test_without_pin_single_player_is_still_auto_selected(self):
        state, result, mocks = _pin_dispatch([_OTHER], state=_pin_batch_state(pinned_player=None))
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _OTHER
        mocks["diag"].assert_called_once_with(_OTHER.online_id, _OTHER.player, None)

    def test_select_nth_still_applies_when_pin_has_no_match(self):
        state, result, _ = _pin_dispatch([_OTHER, _OTHER_EN], select_nth=1)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _OTHER


@pytest.mark.unit
class TestPinnedPlayerFailure:
    def test_resolve_failure_returns_to_player_pick_and_keeps_pin(self):
        state, _, _ = _pin_dispatch([_OTHER, _PIN_A2])
        with patch("alt_ani_cli.cli._resolve_with_fallback", return_value=(None, None)):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.PLAYER_PICK
        assert state.failed_ids == {_PIN_A2.online_id}
        assert state.ep_idx == 1
        assert state.pinned_player == _PIN_FP

        with (
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", _OTHER)) as mock_pick,
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.RESOLVE_STREAM
        assert mock_pick.call_args.kwargs["failed"] == {_PIN_A2.online_id}
        assert state.chosen_player is _OTHER

    def test_download_failed_returns_to_player_pick_and_keeps_pin(self):
        state = _pin_batch_state(players=[_OTHER, _PIN_A2], chosen_player=_PIN_A2)
        result, _, record = _run_download(state, _FakeDownloads(fail_urls={_STREAM.url}))
        assert result is Screen.PLAYER_PICK
        assert state.failed_ids == {_PIN_A2.online_id}
        assert state.ep_idx == 1
        assert state.pinned_player == _PIN_FP
        record.assert_not_called()

    def test_download_target_error_keeps_pin_and_player(self):
        state = _pin_batch_state(players=[_OTHER, _PIN_A2], chosen_player=_PIN_A2)
        with (
            patch("alt_ani_cli.download.run", side_effect=DownloadTargetError("locked")),
            patch("alt_ani_cli.history.record_download") as mock_record,
            patch("alt_ani_cli.ui.progress.error"),
        ):
            assert handle_run_action(state) is Screen.PLAYER_PICK
        assert state.failed_ids == set()
        assert state.ep_idx == 1
        assert state.pinned_player == _PIN_FP
        mock_record.assert_not_called()


def _manual_fallback(picked, *, state=None, pin_choice=None, qualities=None):
    """PLAYER_PICK (manual pick) → RESOLVE_STREAM → ACTION_PICK with download cached and pin active."""
    if state is None:
        state = _pin_batch_state(players=[_PIN_A2, _OTHER, _PIN_A3], chosen_player=None)
    stream = Stream(url=_STREAM.url, ext="m3u8", qualities=qualities or {})
    with (
        patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", picked)),
        patch("alt_ani_cli.cli._resolve_with_fallback", return_value=(stream, _EMBED)),
        patch("alt_ani_cli.ui.menus.select_pin_fallback_action", return_value=pin_choice) as mock_pin,
        patch("alt_ani_cli.ui.menus.select_action") as mock_action,
        patch("alt_ani_cli.ui.progress.warn"),
    ):
        assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.RESOLVE_STREAM
        state.quality = "best"
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        result = HANDLERS[Screen.ACTION_PICK](state)
    mock_action.assert_not_called()
    return state, result, mock_pin


@pytest.mark.unit
class TestPinManualFallback:
    def test_keep_downloads_without_changing_pin(self):
        state, result, mock_pin = _manual_fallback(_OTHER, pin_choice="keep")
        mock_pin.assert_called_once()
        assert result is Screen.RUN_ACTION
        assert state.pinned_player == _PIN_FP
        assert state.episode_action == "download"

    def test_repin_replaces_pin_with_current_player(self):
        state, result, _ = _manual_fallback(_OTHER, pin_choice="repin")
        assert result is Screen.RUN_ACTION
        assert state.pinned_player == player_fingerprint(_OTHER)

    def test_same_fingerprint_manual_pick_does_not_ask(self):
        state, result, mock_pin = _manual_fallback(_PIN_A3)
        mock_pin.assert_not_called()
        assert result is Screen.RUN_ACTION
        assert state.pinned_player == _PIN_FP

    def test_last_target_does_not_ask(self):
        state = _pin_batch_state(ep_idx=2, players=[_PIN_A2, _OTHER], chosen_player=None)
        state, result, mock_pin = _manual_fallback(_OTHER, state=state)
        mock_pin.assert_not_called()
        assert result is Screen.RUN_ACTION
        assert state.pinned_player == _PIN_FP

    def test_esc_returns_to_player_pick_keeping_pin_and_action(self):
        state, result, _ = _manual_fallback(_OTHER, pin_choice=None)
        assert result is Screen.PLAYER_PICK
        assert state.pinned_player == _PIN_FP
        assert state.episode_action == "download"

    def test_esc_with_qualities_returns_to_quality_pick(self):
        _, result, _ = _manual_fallback(_OTHER, pin_choice=None, qualities={"720p": "u"})
        assert result is Screen.QUALITY_PICK

    def test_auto_picked_player_with_other_fingerprint_does_not_ask(self):
        state, _, _ = _pin_dispatch([_OTHER, _OTHER_EN], select_nth=1)
        state.stream = _STREAM
        with patch("alt_ani_cli.ui.menus.select_pin_fallback_action") as mock_pin:
            assert HANDLERS[Screen.ACTION_PICK](state) is Screen.RUN_ACTION
        mock_pin.assert_not_called()

    def test_manual_flag_resets_on_next_episode(self):
        state, _, _ = _manual_fallback(_OTHER, pin_choice="keep")
        assert state.player_picked_manually is True
        state.ep_idx = 2
        _pin_dispatch([_OTHER, _PIN_A2], state=state)
        assert state.player_picked_manually is False


@pytest.mark.unit
class TestPinReset:
    def test_new_episode_batch_clears_pin(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP4, _EP5], pinned_player=_PIN_FP)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[_EP5]):
            HANDLERS[Screen.EPISODES_PICK](state)
        assert state.pinned_player is None

    def test_cli_episode_range_batch_clears_pin(self):
        state = _make_state(ref=_SERIES_REF, episodes=[_EP4, _EP5], pinned_player=_PIN_FP, args=_make_args(episode="4-5"))
        HANDLERS[Screen.EPISODES_PICK](state)
        assert state.pinned_player is None

    def test_cancel_batch_clears_pin(self):
        state = _pin_batch_state(chosen_player=_PIN_A2)
        result, _, _ = _run_download(state, _FakeDownloads(existing={5.0}), "cancel")
        assert result is Screen.EPISODES_PICK
        assert state.pinned_player is None

    def test_esc_from_player_pick_clears_pin(self):
        state = _pin_batch_state()
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)):
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.EPISODES_PICK
        assert state.pinned_player is None

    def test_new_series_clears_pin(self):
        state = _make_state(ref=_SERIES_REF, pinned_player=_PIN_FP)
        with patch("alt_ani_cli.shinden.series.list_episodes", return_value=(_SERIES_REF, [_EP1])):
            HANDLERS[Screen.FETCH_EPISODES](state)
        assert state.pinned_player is None

    @pytest.mark.parametrize("answer", [False, None])
    def test_filter_mismatch_back_to_episode_pick_clears_pin(self, answer):
        state = _pin_batch_state(args=_make_args(lang="xx"))
        state, result, _ = _pin_dispatch([_OTHER, _PIN_A2], state=state, confirm=answer)
        assert result is Screen.EPISODES_PICK
        assert state.pinned_player is None

    def test_quality_pick_keeps_pin(self):
        state = _pin_batch_state(stream=Stream(url=_STREAM.url, ext="m3u8", qualities={"720p": "u"}))
        with patch("alt_ani_cli.ui.menus.select_quality", return_value="720p"):
            HANDLERS[Screen.QUALITY_PICK](state)
        assert state.pinned_player == _PIN_FP

    def test_next_episode_in_batch_keeps_pin(self):
        state = _pin_batch_state(chosen_player=_PIN_A2)
        result, _, _ = _run_download(state, _FakeDownloads())
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 2)
        assert state.pinned_player == _PIN_FP


@pytest.mark.unit
class TestPinIndependentOfOverwritePolicy:
    def test_overwrite_remaining_does_not_pin(self):
        state = _pin_batch_state(pinned_player=None, chosen_player=_PIN_A2)
        _run_download(state, _FakeDownloads(existing={5.0}), "overwrite_remaining")
        assert state.overwrite_existing_batch is True
        assert state.pinned_player is None

    def test_policy_and_pin_both_survive_pinned_download_failure(self):
        state = _pin_batch_state(players=[_OTHER, _PIN_A2], chosen_player=_PIN_A2, overwrite_existing_batch=True)
        _run_download(state, _FakeDownloads(existing={5.0}, fail_urls={_STREAM.url}))
        assert state.overwrite_existing_batch is True
        assert state.pinned_player == _PIN_FP

    def test_repin_keeps_overwrite_policy(self):
        state = _pin_batch_state(players=[_PIN_A2, _OTHER], chosen_player=None, overwrite_existing_batch=True)
        state, _, _ = _manual_fallback(_OTHER, state=state, pin_choice="repin")
        assert state.overwrite_existing_batch is True


@pytest.mark.unit
class TestPinNotPersisted:
    def test_history_file_has_no_pin_data(self):
        from alt_ani_cli import history

        state = _pin_batch_state(chosen_player=_PIN_A2)
        with (
            patch("alt_ani_cli.download.run", side_effect=_FakeDownloads()),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        text = history.HISTORY_FILE.read_text(encoding="utf-8")
        assert "miorosubs" not in text
        assert "pinned" not in text.casefold()
