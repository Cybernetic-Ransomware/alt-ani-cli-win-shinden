from dataclasses import replace
from unittest.mock import patch
from urllib.parse import quote

import pytest

from alt_ani_cli.args import CliOptions
from alt_ani_cli.content import CONTENT
from alt_ani_cli.flow import handlers
from alt_ani_cli.flow.handlers import HANDLERS, _prefetch_player_sources
from alt_ani_cli.flow.state import Screen
from alt_ani_core.config import SHINDEN_XHR_BASE
from alt_ani_core.errors import AntiBotError, ParseError
from alt_ani_core.events import XhrFallback
from alt_ani_core.models import PlayerSource, Purpose, SourceFilter
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.transport.api import TransportTimeout

from .support import (
    EMBED,
    EP1,
    EP2,
    PLAYER,
    PLAYER2,
    PLAYER3,
    PLAYERS_CHANGED,
    SERIES_REF,
    FakeEpisodeSession,
    episode_html,
    fake_engine,
    http,
    iframe,
    in_turn,
    make_state,
    noop_spinner,
    serve_players,
    xhr_error,
)

_PROG = CONTENT["progress"]
_FLOW_TOKEN_1 = "Rmxvd0VwaXNvZGVPbmVUb2tlbg=="
_FLOW_TOKEN_2 = "Rmxvd0VwaXNvZGVUd29Ub2tlbg=="
_INVALID_TOKEN = http(403, '{"error": {"code": "invalid_token"}}')


def _dispatch_state(players=(PLAYER, PLAYER2), *, targets=(EP1,), configure=None, **option_fields):
    """A state ready for EPISODE_DISPATCH on a fake engine whose every opened episode lists ``players``."""
    engine = fake_engine()
    serve_players(engine, players, configure=configure)
    return make_state(
        options=CliOptions(**option_fields), engine=engine, ref=SERIES_REF, targets=list(targets), ep_idx=0, session=None
    )


@pytest.mark.unit
class TestEpisodeDispatch:
    def test_opens_the_episode_and_stores_its_session(self):
        state = _dispatch_state()
        assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.PLAYER_PICK
        state.engine.open_episode.assert_called_once_with(EP1)
        assert state.session is state.engine.opened[0]
        assert handlers._session(state, EP1) is state.session
        assert state.players == [PLAYER, PLAYER2]

    def test_all_targets_done_exits(self):
        state = _dispatch_state()
        state.ep_idx = 1
        assert HANDLERS[Screen.EPISODE_DISPATCH](state) is None
        state.engine.open_episode.assert_not_called()

    def test_episode_without_players_is_skipped(self):
        state = _dispatch_state(players=(), targets=(EP1, EP2))
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1
        mock_warn.assert_called_once_with(_PROG["no_players"].format(number=1.0))

    def test_resets_per_episode_player_state(self):
        state = _dispatch_state()
        state.chosen_player = PLAYER
        state.player_picked_manually = True
        state.failed_ids = {PLAYER.online_id}
        state.expired_ids = {PLAYER2.online_id}
        state.resolved = object()
        HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert (state.chosen_player, state.player_picked_manually, state.resolved) == (None, False, None)
        assert (state.failed_ids, state.expired_ids) == (set(), set())


@pytest.mark.unit
class TestEpisodeDispatchFilterMiss:
    def test_filters_match_proceeds_without_confirm(self):
        state = _dispatch_state(lang="jp")
        with patch("alt_ani_cli.ui.menus.confirm") as mock_confirm:
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_confirm.assert_not_called()
        assert result is Screen.PLAYER_PICK
        assert state.players == [PLAYER, PLAYER2]
        assert state.session.select_calls == [(SourceFilter(lang="jp"), Purpose.WATCH)]

    def test_matching_filter_narrows_the_shown_list(self):
        other = replace(PLAYER3, lang_subs="en")
        state = _dispatch_state(players=(PLAYER, other, PLAYER2), subs="pl")
        assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.PLAYER_PICK
        assert state.players == [PLAYER, PLAYER2]

    def test_filter_miss_confirm_true_uses_full_list(self):
        state = _dispatch_state(lang="xx")
        with patch("alt_ani_cli.ui.menus.confirm", return_value=True) as mock_confirm:
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert "--lang=xx" in mock_confirm.call_args.args[0]
        assert result is Screen.PLAYER_PICK
        assert state.players == [PLAYER, PLAYER2]

    def test_filter_miss_confirm_false_returns_episodes_pick(self):
        state = _dispatch_state(lang="xx")
        with patch("alt_ani_cli.ui.menus.confirm", return_value=False):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert result is Screen.EPISODES_PICK

    def test_filter_miss_confirm_none_esc_returns_episodes_pick(self):
        state = _dispatch_state(lang="xx")
        with patch("alt_ani_cli.ui.menus.confirm", return_value=None):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert result is Screen.EPISODES_PICK

    def test_allow_fallback_skips_confirm_and_warns(self):
        state = _dispatch_state(lang="xx", allow_fallback=True)
        with (
            patch("alt_ani_cli.ui.menus.confirm") as mock_confirm,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_confirm.assert_not_called()
        mock_warn.assert_called_once_with(_PROG["filter_fallback"].format(filters="--lang=xx"))
        assert result is Screen.PLAYER_PICK
        assert state.players == [PLAYER, PLAYER2]


@pytest.mark.unit
class TestEpisodeDispatchSinglePlayerAutoPick:
    def test_auto_picks_only_player_and_logs_diagnostics(self):
        state = _dispatch_state(players=(PLAYER,))
        with patch("alt_ani_core.diagnostics.player_selected") as mock_diag:
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is PLAYER
        assert state.player_picked_manually is False
        mock_diag.assert_called_once_with(PLAYER.online_id, PLAYER.player, None)

    def test_select_nth_auto_picks_the_first_of_several(self):
        state = _dispatch_state(select_nth=1, show_sources=True)
        assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.RESOLVE_STREAM
        assert state.chosen_player is PLAYER
        assert state.session.probe_calls == []


@pytest.mark.unit
class TestEpisodeDispatchDownloadSorting:
    def _purposes(self, state):
        HANDLERS[Screen.EPISODE_DISPATCH](state)
        return [purpose for _, purpose in state.session.select_calls]

    def test_cached_download_action_uses_download_ranking(self):
        state = _dispatch_state()
        state.episode_action = "download"
        with patch("alt_ani_core.rules.ranking.sort_players", return_value=[PLAYER, PLAYER2]) as mock_sort:
            assert self._purposes(state) == [Purpose.DOWNLOAD]
        assert mock_sort.call_args.kwargs == {"download": True}

    def test_download_flag_uses_download_ranking(self):
        assert self._purposes(_dispatch_state(download=True)) == [Purpose.DOWNLOAD]

    def test_play_action_keeps_playback_ranking(self):
        state = _dispatch_state()
        state.episode_action = "play"
        with patch("alt_ani_core.rules.ranking.sort_players", return_value=[PLAYER, PLAYER2]) as mock_sort:
            assert self._purposes(state) == [Purpose.WATCH]
        assert mock_sort.call_args.kwargs == {"download": False}

    def test_download_ranking_orders_the_shown_players(self):
        dub = replace(PLAYER3, lang_audio="pl")
        watch = _dispatch_state(players=(PLAYER, dub))
        HANDLERS[Screen.EPISODE_DISPATCH](watch)
        assert watch.players == [dub, PLAYER]
        download = _dispatch_state(players=(dub, PLAYER), download=True)
        HANDLERS[Screen.EPISODE_DISPATCH](download)
        assert download.players == [PLAYER, dub]


@pytest.mark.unit
class TestHandlePlayerPick:
    def test_esc_returns_episodes_pick(self):
        state = make_state(ref=SERIES_REF, targets=[EP1], ep_idx=0, players=[PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.EPISODES_PICK

    def test_pick_returns_resolve_stream(self):
        state = make_state(ref=SERIES_REF, targets=[EP1], ep_idx=0, players=[PLAYER])
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", PLAYER)):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        assert result is Screen.RESOLVE_STREAM
        assert state.chosen_player is PLAYER
        assert state.player_picked_manually is True

    def test_source_signal_shows_modal_and_rerenders(self):
        state = make_state(ref=SERIES_REF, targets=[EP1], ep_idx=0, players=[PLAYER])
        signals = iter([("source", 0), ("back", None)])
        with (
            patch("alt_ani_cli.ui.menus.select_player_once", side_effect=lambda *a, **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.PLAYER_PICK](state)
        mock_modal.assert_called_once()
        assert result is Screen.EPISODES_PICK

    def test_menu_and_modal_get_the_sources_known_to_the_session(self):
        state = make_state(ref=SERIES_REF, targets=[EP1], ep_idx=0, players=[PLAYER, PLAYER2], failed_ids={"102"})
        source = PlayerSource(PLAYER.online_id, "filemoon.sx", EMBED.url)
        state.session.known_sources = {PLAYER.online_id: source}
        signals = iter([("source", 0), ("pick", PLAYER)])
        with (
            patch("alt_ani_cli.ui.menus.select_player_once", side_effect=lambda *a, **kw: next(signals)) as mock_pick,
            patch("alt_ani_cli.ui.menus.format_player_source", return_value=("t", "b")) as mock_format,
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
            patch("alt_ani_core.diagnostics.player_selected") as mock_diag,
        ):
            HANDLERS[Screen.PLAYER_PICK](state)
        assert mock_pick.call_args.args[0] == [PLAYER, PLAYER2]
        assert mock_pick.call_args.kwargs["sources"] == {PLAYER.online_id: source}
        assert mock_pick.call_args.kwargs["failed"] == {"102"}
        mock_format.assert_called_once_with(PLAYER, source)
        mock_modal.assert_called_once_with("t", "b")
        mock_diag.assert_called_once_with(PLAYER.online_id, PLAYER.player, "filemoon.sx")

    def test_failed_chosen_player_is_announced(self):
        state = make_state(
            ref=SERIES_REF, targets=[EP1], players=[PLAYER, PLAYER2], chosen_player=PLAYER, failed_ids={PLAYER.online_id}
        )
        with (
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            HANDLERS[Screen.PLAYER_PICK](state)
        mock_warn.assert_called_once_with(_PROG["player_failed_short"].format(player=repr(PLAYER.player)))


@pytest.mark.unit
class TestPrefetchPlayerSources:
    def test_success_records_host_and_embed(self, open_state, shinden):
        state = open_state(players=(PLAYER,))
        shinden.player(PLAYER, embed=EMBED.url)
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            assert _prefetch_player_sources(state) is None
        source = state.session.known_sources[PLAYER.online_id]
        assert source.host == "filemoon.sx"
        assert source.embed_url == EMBED.url

    def test_antibot_error_leaves_host_unknown(self, open_state):
        state = open_state()

        def _resolve(client, online_id, **_kwargs):
            if online_id == PLAYER.online_id:
                raise AntiBotError("blocked")
            return EMBED

        with (
            patch("alt_ani_core.shinden.api.resolve_embed", side_effect=_resolve),
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
        ):
            _prefetch_player_sources(state)
        assert set(state.session.known_sources) == {PLAYER2.online_id}

    def test_known_ids_are_skipped(self):
        state = make_state(targets=[EP1], players=[PLAYER])
        state.session.known_sources = {PLAYER.online_id: PlayerSource(PLAYER.online_id, "filemoon.sx", EMBED.url)}
        assert _prefetch_player_sources(state) is None
        assert state.session.probe_calls == []

    def test_only_unknown_players_are_probed(self):
        state = make_state(targets=[EP1], players=[PLAYER, PLAYER2])
        state.session.known_sources = {PLAYER.online_id: PlayerSource(PLAYER.online_id, "filemoon.sx", EMBED.url)}
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            _prefetch_player_sources(state)
        assert state.session.probe_calls == [[PLAYER2]]

    def test_second_prefetch_sends_no_requests(self, open_state, shinden):
        state = open_state()
        shinden.player(PLAYER)
        shinden.player(PLAYER2)
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            _prefetch_player_sources(state)
            sent = len(shinden.transport.requests)
            _prefetch_player_sources(state)
        assert len(shinden.transport.requests) == sent

    def test_stop_is_reported_and_its_outcome_returned(self):
        stop = xhr_error(http_status=429, retry_after=60)
        state = make_state(targets=[EP1], players=[PLAYER, PLAYER2])
        state.session.probe_script = stop
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            outcome = _prefetch_player_sources(state)
        assert outcome is shinden_api.XhrOutcome.ACCESS_BLOCKED
        mock_warn.assert_called_once_with(_PROG["prefetch_stopped"].format(reason=shinden_api.stop_message(stop)))

    def test_refuses_a_session_of_another_episode(self):
        state = make_state(targets=[EP1, EP2], ep_idx=1, players=[PLAYER], session=FakeEpisodeSession(EP1, [PLAYER]))
        with pytest.raises(AssertionError):
            _prefetch_player_sources(state)
        assert state.session.probe_calls == []

    def test_episode_dispatch_triggers_prefetch_with_flag(self):
        state = _dispatch_state(show_sources=True)
        with patch("alt_ani_cli.flow.handlers._prefetch_player_sources") as mock_prefetch:
            result = HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_prefetch.assert_called_once_with(state)
        assert result is Screen.PLAYER_PICK

    def test_episode_dispatch_skips_prefetch_without_flag(self):
        state = _dispatch_state()
        with patch("alt_ani_cli.flow.handlers._prefetch_player_sources") as mock_prefetch:
            HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_prefetch.assert_not_called()
        assert state.session.probe_calls == []

    def test_episode_dispatch_probes_the_shown_players(self):
        state = _dispatch_state(show_sources=True)
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.PLAYER_PICK
        assert state.session.probe_calls == [[PLAYER, PLAYER2]]


@pytest.mark.unit
class TestPrefetchPlayerSourcesXhrStop:
    @pytest.mark.parametrize(
        "load",
        [http(429), http(503), TransportTimeout(), "abc"],
        ids=["429", "503", "timeout", "invalid-wait"],
    )
    def test_stop_sends_no_further_xhr(self, open_state, shinden, load):
        state = open_state(players=(PLAYER, PLAYER2, PLAYER3))
        shinden.player(PLAYER)
        shinden.player(PLAYER2, load=load)
        shinden.player(PLAYER3)
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            outcome = _prefetch_player_sources(state)
        assert outcome is not None
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id, PLAYER2.online_id]
        assert shinden.xhr_ids("player_show") == [PLAYER.online_id]
        assert set(state.session.known_sources) == {PLAYER.online_id}
        mock_warn.assert_called_once()

    def test_player_local_failure_continues_sequentially(self, open_state, shinden):
        state = open_state(players=(PLAYER, PLAYER2, PLAYER3))
        shinden.player(PLAYER, show="<html><body></body></html>")
        shinden.player(PLAYER2)
        shinden.player(PLAYER3)
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            assert _prefetch_player_sources(state) is None
        assert shinden.xhr_ids("player_load") == ["101", "102", "103"]
        assert set(state.session.known_sources) == {"102", "103"}
        mock_warn.assert_not_called()


@pytest.mark.unit
class TestEpisodeXhrState:
    def _state(self, make_engine, backend="new"):
        return make_state(engine=make_engine(xhr_backend=backend), ref=SERIES_REF, targets=[EP1, EP2], ep_idx=0, session=None)

    def test_dispatch_opens_the_page_context_for_the_current_episode(self, make_engine, shinden, sink, token_page):
        state = self._state(make_engine)
        shinden.episode(EP1, token_page(_FLOW_TOKEN_1))
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.PLAYER_PICK
        session = handlers._session(state, EP1)
        assert session.episode == EP1
        assert {p.online_id for p in session.sources} == {p.online_id for p in state.players}
        assert sink.of(XhrFallback) == []
        mock_warn.assert_not_called()
        assert _FLOW_TOKEN_1 not in repr(state)
        assert _FLOW_TOKEN_1 not in repr(session.__dict__)

        first = state.players[0]
        shinden.player(first)
        assert session.probe_hosts([first]) is None
        assert shinden.transport.urls[-2].startswith(f"{SHINDEN_XHR_BASE}/{first.online_id}/player_load?")

    @pytest.mark.parametrize("backend", ["new", "api4"])
    def test_drifted_page_is_an_error_not_an_empty_player_list(self, make_engine, shinden, token_page, backend):
        state = self._state(make_engine, backend)
        shinden.episode(EP1, token_page(_FLOW_TOKEN_1).replace('"pulapka": 2045542847', '"renamed": 1', 1))
        with (
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
            pytest.raises(ParseError, match="honeypot"),
        ):
            HANDLERS[Screen.EPISODE_DISPATCH](state)
        mock_warn.assert_not_called()
        assert shinden.transport.urls == [EP1.url]
        assert state.ep_idx == 0
        assert state.session is None

    def test_honeypot_is_never_offered_or_requested(self, make_engine, shinden, token_page):
        state = self._state(make_engine)
        state.options = CliOptions(show_sources=True)
        shinden.episode(EP1, token_page(_FLOW_TOKEN_1))
        shinden.transport.route(lambda request: "/player_load" in request.url, "0")
        shinden.transport.route(lambda request: "/player_show" in request.url, iframe(EMBED.url))
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.PLAYER_PICK
        assert "2045542847" not in {p.online_id for p in state.players}
        assert "2045542847" not in shinden.xhr_ids("player_load")
        assert shinden.xhr_ids("player_load") == [p.online_id for p in state.players]

    def test_next_episode_gets_a_new_context(self, make_engine, shinden, token_page):
        state = self._state(make_engine)
        shinden.episode(EP1, token_page(_FLOW_TOKEN_1))
        shinden.episode(EP2, token_page(_FLOW_TOKEN_2))
        HANDLERS[Screen.EPISODE_DISPATCH](state)
        first = state.session
        state.ep_idx = 1
        HANDLERS[Screen.EPISODE_DISPATCH](state)
        second = state.session
        assert first is not None and second is not None
        assert first is not second
        assert second.episode.url == EP2.url

        player = state.players[0]
        shinden.player(player)
        assert second.probe_hosts([player]) is None
        xhr_urls = [url for url in shinden.transport.urls if "/xhr/" in url]
        assert all(f"auth={quote(_FLOW_TOKEN_2, safe='')}" in url for url in xhr_urls)
        assert not any(quote(_FLOW_TOKEN_1, safe="") in url for url in xhr_urls)

    def test_resolve_stream_refuses_a_context_from_another_episode(self):
        stale = FakeEpisodeSession(EP1, [PLAYER])
        state = make_state(targets=[EP1, EP2], ep_idx=1, players=[PLAYER], chosen_player=PLAYER, session=stale)
        with pytest.raises(AssertionError):
            HANDLERS[Screen.RESOLVE_STREAM](state)
        assert stale.resolve_calls == []

    def test_players_changed_reloads_the_episode(self):
        state = make_state(targets=[EP1], ep_idx=0, players=[PLAYER, PLAYER2], chosen_player=PLAYER)
        state.session.resolve_script = PLAYERS_CHANGED
        with (
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.info"),
        ):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.EPISODE_DISPATCH
        assert state.reload_pending is True
        assert state.failed_ids == set()
        assert state.ep_idx == 0
        assert "player list" in mock_error.call_args.args[0]

    def test_prefetch_shares_one_refresh_and_stops_on_a_second_invalid_token(self, make_engine, shinden, token_page):
        engine = make_engine(xhr_backend="new")
        shinden.episode(EP1, in_turn(token_page(_FLOW_TOKEN_1), token_page(_FLOW_TOKEN_2)))
        session = engine.open_episode(EP1)
        first, second = session.sources[0], session.sources[1]
        state = make_state(engine=engine, targets=[EP1], ep_idx=0, players=[first, second], session=session)
        shinden.player(first, embed="https://h.example/e/1", load=in_turn(_INVALID_TOKEN, "0"))
        shinden.player(second, load=_INVALID_TOKEN)
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            outcome = _prefetch_player_sources(state)
        assert outcome is shinden_api.XhrOutcome.ACCESS_BLOCKED
        assert shinden.page_gets(EP1) == 2
        assert set(session.known_sources) == {first.online_id}
        text = mock_warn.call_args.args[0]
        assert "blocked" in text
        assert _FLOW_TOKEN_1 not in text and _FLOW_TOKEN_2 not in text
        assert quote(_FLOW_TOKEN_2, safe="") not in text


@pytest.mark.unit
class TestPlayersChangedReloadLimit:
    def _state(self, targets=(EP1,), show_sources=True, probe=PLAYERS_CHANGED, resolve=PLAYERS_CHANGED):
        def configure(session):
            session.probe_script = probe
            session.resolve_script = resolve

        state = _dispatch_state(targets=targets, configure=configure, show_sources=show_sources)
        state.episodes = list(targets)
        return state

    def _used_session(self, ep=EP1):
        session = FakeEpisodeSession(ep, [PLAYER, PLAYER2], probe=PLAYERS_CHANGED, resolve=PLAYERS_CHANGED)
        session.reload_used = True
        return session

    def _limit_warned(self, mock_warn, number=1.0) -> bool:
        limit = _PROG["players_reload_limit"].format(number=number)
        return any(c.args[0] == limit for c in mock_warn.call_args_list)

    def _run(self, state, screen):
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.info"),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            return HANDLERS[screen](state)

    def test_prefetch_players_changed_reloads_the_episode(self):
        state = self._state()
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.EPISODE_DISPATCH
        assert len(state.session.probe_calls) == 1
        assert state.reload_pending is True
        assert state.session.reload_calls == 0
        assert state.ep_idx == 0
        assert not self._limit_warned(mock_warn)

    def test_reload_happens_in_place_on_the_same_session(self):
        state = self._state(probe=[PLAYERS_CHANGED, None])
        with patch("alt_ani_cli.ui.progress.warn"):
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.EPISODE_DISPATCH
            session = state.session
            session.reloaded_sources = [PLAYER2, PLAYER3]
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
        assert state.session is session
        assert session.reload_calls == 1
        assert state.reload_pending is False
        state.engine.open_episode.assert_called_once_with(EP1)
        assert state.players == [PLAYER2, PLAYER3]

    def test_other_prefetch_stop_does_not_reload(self):
        state = self._state(probe=xhr_error(http_status=403))
        with patch("alt_ani_cli.ui.progress.warn"):
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
        assert state.reload_pending is False
        assert state.session.reload_calls == 0
        state.engine.open_episode.assert_called_once()

    def test_second_change_stops_without_another_get(self):
        state = self._state()
        screen = Screen.EPISODE_DISPATCH
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            for _ in range(5):
                screen = self._run(state, screen)
                if screen is not Screen.EPISODE_DISPATCH:
                    break
        assert screen is Screen.PLAYER_PICK
        state.engine.open_episode.assert_called_once()
        assert state.session.reload_calls == 1
        assert len(state.session.probe_calls) == 2
        assert state.reload_pending is False
        assert self._limit_warned(mock_warn)

    def test_limit_is_shared_between_resolve_stream_and_prefetch(self):
        state = self._state(show_sources=False)
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
            state.chosen_player = PLAYER
            assert self._run(state, Screen.RESOLVE_STREAM) is Screen.EPISODE_DISPATCH
            state.options = replace(state.options, show_sources=True)
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
            state.chosen_player = PLAYER
            assert self._run(state, Screen.RESOLVE_STREAM) is Screen.PLAYER_PICK
        state.engine.open_episode.assert_called_once()
        assert state.session.reload_calls == 1
        assert self._limit_warned(mock_warn)

    def test_prefetch_reload_then_resolve_stream_change_stops(self):
        state = self._state()
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.EPISODE_DISPATCH
            state.options = replace(state.options, show_sources=False)
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
            state.chosen_player = PLAYER
            assert self._run(state, Screen.RESOLVE_STREAM) is Screen.PLAYER_PICK
        state.engine.open_episode.assert_called_once()
        assert state.session.reload_calls == 1
        assert state.failed_ids == set()
        assert self._limit_warned(mock_warn)

    def test_limit_resets_for_another_episode(self):
        state = self._state(targets=(EP1, EP2))
        used = self._used_session(EP1)
        state.session = used
        state.ep_idx = 1
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.EPISODE_DISPATCH
        state.engine.open_episode.assert_called_once_with(EP2)
        assert state.session is not used
        assert state.session.episode == EP2
        assert state.reload_pending is True
        assert not self._limit_warned(mock_warn, number=2.0)

    def test_pending_reload_of_another_episode_opens_a_new_session(self):
        state = self._state(targets=(EP1, EP2), probe=None)
        stale = FakeEpisodeSession(EP1, [PLAYER, PLAYER2])
        state.session = stale
        state.reload_pending = True
        state.ep_idx = 1
        with patch("alt_ani_cli.ui.progress.warn"):
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
        assert stale.reload_calls == 0
        state.engine.open_episode.assert_called_once_with(EP2)
        assert state.reload_pending is False

    def test_limit_resets_on_a_new_episode_pick(self):
        state = self._state()
        used = self._used_session(EP1)
        state.session = used
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP1]):
            assert HANDLERS[Screen.EPISODES_PICK](state) is Screen.EPISODE_DISPATCH
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.EPISODE_DISPATCH
        state.engine.open_episode.assert_called_once_with(EP1)
        assert state.session is not used
        assert state.session.reload_used is False
        assert used.reload_calls == 0
        assert state.reload_pending is True
        assert not self._limit_warned(mock_warn)

    def test_repicking_the_same_episode_after_a_reload_gets_a_fresh_limit(self):
        state = self._state(show_sources=False)
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
            state.chosen_player = PLAYER
            assert self._run(state, Screen.RESOLVE_STREAM) is Screen.EPISODE_DISPATCH
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
            state.chosen_player = PLAYER
            assert self._run(state, Screen.RESOLVE_STREAM) is Screen.PLAYER_PICK
            assert self._limit_warned(mock_warn)
            first = state.session

            mock_warn.reset_mock()
            with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)):
                assert self._run(state, Screen.PLAYER_PICK) is Screen.EPISODES_PICK
            with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP1]):
                assert self._run(state, Screen.EPISODES_PICK) is Screen.EPISODE_DISPATCH
            assert self._run(state, Screen.EPISODE_DISPATCH) is Screen.PLAYER_PICK
            state.chosen_player = PLAYER
            assert self._run(state, Screen.RESOLVE_STREAM) is Screen.EPISODE_DISPATCH
        assert state.engine.open_episode.call_count == 2
        assert state.session is not first
        assert state.reload_pending is True
        assert not self._limit_warned(mock_warn)


@pytest.mark.unit
class TestPlayersChangedOnRealEngine:
    def test_refreshed_page_without_the_player_reloads_the_list_in_place(self, make_engine, shinden):
        before = episode_html([PLAYER, PLAYER2], token=_FLOW_TOKEN_1)
        after = episode_html([PLAYER2, PLAYER3], token=_FLOW_TOKEN_2)
        shinden.episode(EP1, in_turn(before, after))
        shinden.player(PLAYER, load=_INVALID_TOKEN)
        shinden.player(PLAYER2)
        shinden.player(PLAYER3)
        state = make_state(
            options=CliOptions(show_sources=True),
            engine=make_engine(xhr_backend="new"),
            ref=SERIES_REF,
            targets=[EP1],
            ep_idx=0,
            session=None,
        )
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.info"),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.EPISODE_DISPATCH
            session = state.session
            assert state.reload_pending is True
            assert shinden.page_gets(EP1) == 2
            assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.PLAYER_PICK

        assert state.session is session
        assert session.reload_used is True
        assert shinden.page_gets(EP1) == 3
        assert state.players == [PLAYER2, PLAYER3]
        assert set(session.known_sources) == {PLAYER2.online_id, PLAYER3.online_id}
        assert shinden.xhr_ids("player_load") == ["101", "102", "103"]
        mock_warn.assert_called_once()
        assert "player list" in mock_warn.call_args.args[0]
        assert _FLOW_TOKEN_1 not in mock_warn.call_args.args[0]
