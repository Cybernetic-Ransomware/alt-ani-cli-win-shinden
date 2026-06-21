"""Tests for FSM navigation — ESC = go back.

Unit tests for individual handlers and integration tests via _run_interactive.
All external I/O (menus, shinden API, history) is mocked.
"""

import argparse
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from alt_ani_cli.errors import ShindenError
from alt_ani_cli.flow.handlers import HANDLERS, _prefetch_series_metadata, _safe_fetch_one, _sorted_by_date_desc
from alt_ani_cli.flow.state import BACK, FlowState, Screen, _BackSentinel
from alt_ani_cli.models import RelatedSeries, SeriesMetadata
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


@pytest.mark.unit
class TestHandlePlayerPick:
    def test_esc_returns_episodes_pick(self):
        state = _make_state(ref=_SERIES_REF, targets=[_EP1], ep_idx=0, players=[_PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player", return_value=None):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.EPISODES_PICK

    def test_pick_returns_resolve_stream(self):
        state = _make_state(ref=_SERIES_REF, targets=[_EP1], ep_idx=0, players=[_PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player", return_value=_PLAYER):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is _PLAYER


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

        def _fake_start(has_history, history_count=0):
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

        def _fake_start(has_history, history_count=0):
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

        player_calls: list[int] = []

        def _fake_player(players, prompt="", failed=None):
            player_calls.append(1)
            return _PLAYER if len(player_calls) == 1 else None

        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER, _PLAYER2]),
            patch("alt_ani_cli.cli._resolve_with_fallback", return_value=(mock_stream, MagicMock())),
            patch("alt_ani_cli.ui.menus.select_player", side_effect=_fake_player),
            patch("alt_ani_cli.ui.menus.select_action", return_value="play"),
            patch("alt_ani_cli.player.runner.play"),
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
        hits = [
            SeriesHit(id=str(i), slug=f"s{i}", title=f"S{i}", url=f"http://shinden.pl/series/{i}-s{i}")
            for i in range(3)
        ]
        same_meta = SeriesMetadata(air_date="2020", air_date_sort=(2020, 1, 1), description="", tags=(), related=())
        metadata = {str(i): same_meta for i in range(3)}

        result = _sorted_by_date_desc(hits, metadata)
        assert [h.id for h in result] == ["0", "1", "2"]


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
