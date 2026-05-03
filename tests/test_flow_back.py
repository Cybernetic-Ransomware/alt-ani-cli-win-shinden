"""Tests for FSM navigation — ESC = go back.

Unit tests for individual handlers and integration tests via _run_interactive.
All external I/O (menus, shinden API, history) is mocked.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.flow.handlers import HANDLERS
from alt_ani_cli.flow.state import BACK, FlowState, Screen, _BackSentinel
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, SeriesHit, SeriesRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Handler unit tests
# ---------------------------------------------------------------------------


class TestHandleStartMode:
    def test_esc_returns_back(self):
        state = _make_state()
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None):
            with patch("alt_ani_cli.history.list_all", return_value=[]):
                result = HANDLERS[Screen.START_MODE](state)
        assert isinstance(result, _BackSentinel)

    def test_quit_returns_none(self):
        state = _make_state()
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value="quit"):
            with patch("alt_ani_cli.history.list_all", return_value=[]):
                result = HANDLERS[Screen.START_MODE](state)
        assert result is None

    def test_search_returns_search_query(self):
        state = _make_state()
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value="search"):
            with patch("alt_ani_cli.history.list_all", return_value=[]):
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


class TestHandleSeriesPick:
    def test_esc_returns_back(self):
        state = _make_state(query="fate")
        with patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]):
            with patch("alt_ani_cli.ui.menus.select_series", return_value=None):
                result = HANDLERS[Screen.SERIES_PICK](state)
        assert isinstance(result, _BackSentinel)

    def test_pick_sets_ref_and_returns_fetch(self):
        state = _make_state(query="fate")
        with patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]):
            with patch("alt_ani_cli.ui.menus.select_series", return_value=_SERIES_HIT):
                with patch("alt_ani_cli.shinden.series.parse_series_url", return_value=_SERIES_REF):
                    result = HANDLERS[Screen.SERIES_PICK](state)
        assert result is Screen.FETCH_EPISODES
        assert state.ref is not None


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


class TestHandlePlayerPick:
    def test_esc_returns_episodes_pick(self):
        state = _make_state(ref=_SERIES_REF, targets=[_EP1], ep_idx=0, players=[_PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player", return_value=None):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.EPISODES_PICK  # ESC → back to episode list

    def test_pick_returns_resolve_stream(self):
        state = _make_state(ref=_SERIES_REF, targets=[_EP1], ep_idx=0, players=[_PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player", return_value=_PLAYER):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _PLAYER


class TestHandleQualityPick:
    def test_esc_returns_player_pick_and_resets_quality(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {"1080p": "url"}
        state = _make_state(stream=mock_stream, quality="1080p")
        with patch("alt_ani_cli.ui.menus.select_quality", return_value=None):
            result = HANDLERS[Screen.QUALITY_PICK](state)
        assert result is Screen.PLAYER_PICK  # ESC → back to player
        assert state.quality is None

    def test_pick_caches_quality(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {"720p": "url"}
        state = _make_state(stream=mock_stream)
        with patch("alt_ani_cli.ui.menus.select_quality", return_value="720p"):
            result = HANDLERS[Screen.QUALITY_PICK](state)
        assert result is Screen.ACTION_PICK
        assert state.quality == "720p"


class TestHandleActionPick:
    def test_esc_resets_action_and_returns_player_pick(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {}
        state = _make_state(stream=mock_stream)
        with patch("alt_ani_cli.ui.menus.select_action", return_value=None):
            result = HANDLERS[Screen.ACTION_PICK](state)
        assert result is Screen.PLAYER_PICK  # no qualities → back to player
        assert state.episode_action is None

    def test_esc_returns_quality_pick_when_qualities_present(self):
        mock_stream = MagicMock()
        mock_stream.qualities = {"720p": "url"}
        state = _make_state(stream=mock_stream)
        with patch("alt_ani_cli.ui.menus.select_action", return_value=None):
            result = HANDLERS[Screen.ACTION_PICK](state)
        assert result is Screen.QUALITY_PICK  # has qualities → back to quality

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


# ---------------------------------------------------------------------------
# Integration tests via _run_interactive
# ---------------------------------------------------------------------------


def _run_interactive_wrapped(args, client):
    """Thin wrapper to call _run_interactive with sys.exit suppressed."""
    from alt_ani_cli.cli import _run_interactive
    _run_interactive(args, client)


class TestInteractiveFlow:
    def test_esc_from_start_exits_silently(self):
        """ESC at START_MODE (empty history_stack) → loop ends, no sys.exit."""
        args = _make_args()
        client = MagicMock()
        with patch("alt_ani_cli.history.list_all", return_value=[]):
            with patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None):
                _run_interactive_wrapped(args, client)  # must not raise

    def test_esc_from_search_query_returns_to_start(self):
        """flow: START_MODE→search→SEARCH_QUERY→ESC→START_MODE (called twice)."""
        args = _make_args()
        client = MagicMock()
        call_count = {"n": 0}

        def _fake_start(has_history, history_count=0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "search"
            return None  # second call → ESC → exit

        with patch("alt_ani_cli.history.list_all", return_value=[]):
            with patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=_fake_start):
                with patch("alt_ani_cli.ui.menus.prompt_search_query", return_value=None):
                    _run_interactive_wrapped(args, client)

        assert call_count["n"] == 2

    def test_esc_from_series_pick_returns_to_search_query(self):
        """flow: START→search→SEARCH_QUERY(fate)→SERIES_PICK→ESC→SEARCH_QUERY(ESC)→START(ESC)→exit."""
        args = _make_args()
        client = MagicMock()
        query_calls = {"n": 0}
        start_calls = {"n": 0}

        def _fake_start(has_history, history_count=0):
            start_calls["n"] += 1
            if start_calls["n"] == 1:
                return "search"
            return None  # second call at START_MODE → ESC → exit

        def _fake_search_query():
            query_calls["n"] += 1
            if query_calls["n"] == 1:
                return "fate"
            return None  # second call → ESC → back to START_MODE

        with patch("alt_ani_cli.history.list_all", return_value=[]):
            with patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=_fake_start):
                with patch("alt_ani_cli.ui.menus.prompt_search_query", side_effect=_fake_search_query):
                    with patch("alt_ani_cli.shinden.search.search_series", return_value=[_SERIES_HIT]):
                        with patch("alt_ani_cli.ui.menus.select_series", return_value=None):
                            _run_interactive_wrapped(args, client)

        assert query_calls["n"] == 2  # called twice: first returns "fate", second ESC
        assert start_calls["n"] == 2  # returned to START_MODE after back from SEARCH_QUERY

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

        player_calls: list[int] = []

        def _fake_player(players, prompt="", failed=None):
            player_calls.append(1)
            return _PLAYER if len(player_calls) == 1 else None  # ESC on second call

        with patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]):
            with patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]):
                with patch("alt_ani_cli.cli._resolve_with_fallback", return_value=(mock_stream, MagicMock())):
                    with patch("alt_ani_cli.ui.menus.select_player", side_effect=_fake_player):
                        with patch("alt_ani_cli.ui.menus.select_action", return_value="play"):
                            with patch("alt_ani_cli.player.runner.play"):
                                with patch("alt_ani_cli.history.upsert"):
                                    # Drive handlers manually: ep1 plays, ep2 ESC
                                    screen = Screen.EPISODE_DISPATCH
                                    for _ in range(30):  # safety limit against infinite loop
                                        result = HANDLERS[screen](state)
                                        if result is Screen.EPISODES_PICK:
                                            final_screen = Screen.EPISODES_PICK
                                            break
                                        screen = result
                                    else:
                                        pytest.fail("Handler loop did not return to EPISODES_PICK")

        assert final_screen is Screen.EPISODES_PICK
        assert 1.0 in state.completed_eps  # ep1 was completed
        assert state.ep_idx == 1           # ep2 index preserved (not incremented past)
