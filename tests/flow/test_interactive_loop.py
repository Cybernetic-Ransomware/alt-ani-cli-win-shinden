from unittest.mock import patch

import pytest

from alt_ani_cli.args import CliOptions
from alt_ani_cli.flow import handlers
from alt_ani_cli.flow.handlers import HANDLERS, run_interactive
from alt_ani_cli.flow.state import FlowState, Screen
from alt_ani_cli.player.runner import PlayResult
from alt_ani_core.models import EpisodeListing

from .support import (
    EMBED,
    EP1,
    EP2,
    PLAYER,
    PLAYER2,
    SERIES_REF,
    STREAM,
    episode_html,
    iframe,
    make_state,
    noop_spinner,
)

_EXTRACT = "alt_ani_core.services.episode_session.extract.resolve"
_SEARCH_URL = "https://shinden.pl/series"
_TOKEN = "TG9vcEVwaXNvZGVUb2tlbg=="


@pytest.mark.unit
class TestInteractiveFlow:
    def test_esc_from_start_exits_silently(self, make_engine, cli_paths, transport):
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None) as mock_start:
            run_interactive(CliOptions(), make_engine(), cli_paths)
        mock_start.assert_called_once_with(watch_count=0, download_count=0)
        assert transport.requests == []

    def test_quit_exits(self, make_engine, cli_paths):
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value="quit") as mock_start:
            run_interactive(CliOptions(), make_engine(), cli_paths)
        mock_start.assert_called_once()

    def test_esc_from_search_query_returns_to_start(self, make_engine, cli_paths):
        """flow: START_MODE→search→SEARCH_QUERY→ESC→START_MODE (called twice)."""
        starts = iter(["search", None])
        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=lambda **kw: next(starts)) as mock_start,
            patch("alt_ani_cli.ui.menus.prompt_search_query", return_value=None),
        ):
            run_interactive(CliOptions(), make_engine(), cli_paths)

        assert mock_start.call_count == 2

    def test_esc_from_series_pick_returns_to_search_query(self, make_engine, cli_paths, transport, shinden_html):
        """flow: START→search→SEARCH_QUERY(fate)→SERIES_PICK→ESC→SEARCH_QUERY(ESC)→START(ESC)→exit."""
        transport.route(lambda request: request.url == _SEARCH_URL, shinden_html("search_frieren.html"))
        starts = iter(["search", None])
        queries = iter(["fate", None])

        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=lambda **kw: next(starts)) as mock_start,
            patch("alt_ani_cli.ui.menus.prompt_search_query", side_effect=lambda: next(queries)) as mock_query,
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={}),
            patch("alt_ani_cli.ui.menus.select_series_once", return_value=("back", None)) as mock_series,
        ):
            run_interactive(CliOptions(), make_engine(), cli_paths)

        assert mock_query.call_count == 2
        assert mock_start.call_count == 2
        assert mock_series.call_args.args[0]
        assert [request.params for request in transport.requests] == [{"search": "fate"}]

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            (CliOptions(resume=True, download=True), "download_history_empty"),
            (CliOptions(resume=True), "history_empty"),
        ],
    )
    def test_resume_flag_with_empty_history_falls_back_to_the_menu_once(self, make_engine, cli_paths, options, message):
        """-c/-c -d with nothing to resume: one error, then the start menu (ESC exits) instead of an endless loop."""
        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None) as mock_start,
            patch("alt_ani_cli.ui.progress.error") as mock_err,
        ):
            run_interactive(options, make_engine(), cli_paths)
        mock_err.assert_called_once_with(handlers._PROG[message])
        mock_start.assert_called_once()

    def test_esc_from_series_pick_of_a_query_flag_shows_the_menu(self, make_engine, cli_paths, transport, shinden_html):
        transport.route(lambda request: request.url == _SEARCH_URL, shinden_html("search_frieren.html"))
        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None) as mock_start,
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={}),
            patch("alt_ani_cli.ui.menus.select_series_once", return_value=("back", None)) as mock_series,
        ):
            run_interactive(CliOptions(query=["frieren"]), make_engine(), cli_paths)
        mock_series.assert_called_once()
        mock_start.assert_called_once()
        assert len(transport.requests) == 1

    def test_quality_option_seeds_the_cached_quality(self, make_engine, cli_paths):
        captured = {}

        def capture(**kwargs):
            captured["state"] = FlowState(**kwargs)
            return captured["state"]

        engine = make_engine()
        options = CliOptions(quality="720p")
        with (
            patch("alt_ani_cli.flow.handlers.FlowState", side_effect=capture),
            patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None),
        ):
            run_interactive(options, engine, cli_paths)
        state = captured["state"]
        assert (state.options, state.engine, state.paths, state.quality) == (options, engine, cli_paths, "720p")

    def test_completed_eps_preserved_after_back(self, make_engine, shinden):
        """After ESC from PLAYER_PICK on ep2, completed_eps has ep1 and ep_idx stays at 1."""
        engine = make_engine(xhr_backend="api4")
        for ep in (EP1, EP2):
            shinden.episode(ep, episode_html([PLAYER, PLAYER2]))
        shinden.player(PLAYER)
        state = make_state(
            engine=engine, ref=SERIES_REF, episodes=[EP1, EP2], targets=[EP1, EP2], ep_idx=0, completed_eps=set(), session=None
        )
        player_calls: list[int] = []

        def _fake_player(players, prompt="", failed=None, sources=None):
            player_calls.append(1)
            return ("pick", players[0]) if len(player_calls) == 1 else ("back", None)

        with (
            patch(_EXTRACT, return_value=STREAM),
            patch("alt_ani_cli.ui.menus.select_player_once", side_effect=_fake_player),
            patch("alt_ani_cli.ui.menus.select_action", return_value="play"),
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)) as mock_play,
        ):
            screen = Screen.EPISODE_DISPATCH
            for _ in range(30):
                result = HANDLERS[screen](state)
                if result is Screen.EPISODES_PICK:
                    break
                screen = result
            else:
                pytest.fail("Handler loop did not return to EPISODES_PICK")

        mock_play.assert_called_once()
        assert 1.0 in state.completed_eps
        assert state.ep_idx == 1
        assert [(e.series, e.last_ep) for e in engine.history.watch_entries()] == [(SERIES_REF, 1.0)]
        assert shinden.page_gets(EP1) == 1 and shinden.page_gets(EP2) == 1

    def test_search_to_playback_end_to_end(self, make_engine, cli_paths, transport, shinden_html, token_page):
        """query → series → episodes → player → resolve → play → exit, on fixture pages with nothing but UI mocked."""
        transport.route(lambda request: "/episode/" in request.url, token_page(_TOKEN))
        transport.route(lambda request: request.url == _SEARCH_URL, shinden_html("search_frieren.html"))
        transport.route(lambda request: "/xhr/" in request.url and "/player_load" in request.url, "0")
        transport.route(lambda request: "/xhr/" in request.url and "/player_show" in request.url, iframe(EMBED.url))
        transport.route(
            lambda request: request.url.endswith("/episodes") and request.profile == "shinden",
            shinden_html("episodes_frieren.html"),
        )
        transport.route(
            lambda request: "/episode/" not in request.url and request.params is None and "/xhr/" not in request.url,
            shinden_html("series_frieren.html"),
        )
        engine = make_engine(xhr_backend="new")
        picked: dict = {}

        def pick_series(hits, metadata=None, **_kw):
            picked["hit"] = hits[0]
            picked["metadata"] = metadata
            return ("pick", hits[0])

        def pick_episode(episodes, **_kw):
            picked["episode"] = episodes[0]
            return [episodes[0]]

        def pick_player(players, **kw):
            picked["player"] = players[0]
            return ("pick", players[0])

        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
            patch("alt_ani_cli.ui.menus.select_start_mode") as mock_start,
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=pick_series),
            patch("alt_ani_cli.ui.menus.select_episodes", side_effect=pick_episode),
            patch("alt_ani_cli.ui.menus.select_player_once", side_effect=pick_player),
            patch("alt_ani_cli.ui.menus.select_action", return_value="play"),
            patch(_EXTRACT, return_value=STREAM) as mock_extract,
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.0)) as mock_play,
        ):
            run_interactive(CliOptions(query=("frieren",)), engine, cli_paths)

        mock_start.assert_not_called()
        mock_warn.assert_not_called()
        assert picked["metadata"][picked["hit"].id].air_date_sort is not None
        assert mock_extract.call_args.args[:2] == (EMBED.url, "https://shinden.pl/")
        assert mock_play.call_args.args[0] == STREAM.default
        assert mock_play.call_args.kwargs["title"].endswith(f"Odcinek {picked['episode'].number:g}")
        [entry] = engine.history.watch_entries()
        assert (entry.series.id, entry.last_ep) == (picked["hit"].id, picked["episode"].number)
        xhr_urls = [url for url in transport.urls if "/xhr/" in url]
        assert len(xhr_urls) == 2
        assert all(f"/xhr/{picked['player'].online_id}/" in url for url in xhr_urls)


@pytest.mark.unit
class TestDownloadResumeBackToSearchRegression:
    def test_back_from_download_resume_to_search_clears_download_mode(self, make_engine, cli_paths):
        """START→resume_download→pick→EPISODES_PICK(ESC)→DOWNLOAD_RESUME_PICK(ESC)→START→search."""
        captured: dict = {}
        seen_at_search: dict = {}
        seen_at_episodes: dict = {}
        starts = iter(["resume_download", "search", None])
        download_picks = iter([(SERIES_REF, frozenset({1.0})), None])
        engine = make_engine()
        engine.history.record_download(SERIES_REF, 1.0)

        def _factory(**kw):
            captured["state"] = FlowState(**kw)
            return captured["state"]

        def _fake_episodes(episodes, **kw):
            s = captured["state"]
            seen_at_episodes.update(
                resume_mode=s.resume_mode, episode_action=s.episode_action, downloaded=kw["downloaded_numbers"]
            )
            return None

        def _fake_search_query():
            s = captured["state"]
            seen_at_search.update(
                resume_mode=s.resume_mode, episode_action=s.episode_action, downloaded_eps=set(s.downloaded_eps)
            )
            return None

        with (
            patch("alt_ani_cli.flow.handlers.FlowState", side_effect=_factory),
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=lambda **kw: next(starts)) as mock_start,
            patch(
                "alt_ani_cli.ui.menus.select_series_from_download_history", side_effect=lambda e: next(download_picks)
            ) as mock_history,
            patch("alt_ani_core.shinden.series.list_episodes", return_value=EpisodeListing(SERIES_REF, [EP1, EP2])),
            patch("alt_ani_cli.ui.menus.select_episodes", side_effect=_fake_episodes),
            patch("alt_ani_cli.ui.menus.prompt_search_query", side_effect=_fake_search_query),
        ):
            run_interactive(CliOptions(), engine, cli_paths)

        assert mock_start.call_args_list[0].kwargs == {"watch_count": 0, "download_count": 1}
        assert mock_history.call_args.args[0] == [(SERIES_REF, frozenset({1.0}))]
        assert seen_at_episodes == {"resume_mode": "download", "episode_action": "download", "downloaded": {1.0}}
        assert seen_at_search == {"resume_mode": None, "episode_action": None, "downloaded_eps": set()}


@pytest.mark.unit
class TestHandlerTable:
    def test_every_screen_has_a_handler(self):
        assert set(HANDLERS) == set(Screen)

    def test_handler_errors_propagate_out_of_the_loop(self, make_engine, cli_paths):
        with (
            patch.dict(handlers.HANDLERS, {Screen.START_MODE: lambda state: 1 / 0}),
            pytest.raises(ZeroDivisionError),
        ):
            run_interactive(CliOptions(), make_engine(), cli_paths)
