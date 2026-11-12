from unittest.mock import patch

import pytest

from alt_ani_cli.args import CliOptions
from alt_ani_cli.content import CONTENT
from alt_ani_cli.flow.handlers import HANDLERS
from alt_ani_cli.flow.state import Screen
from alt_ani_core.config import ExtractOptions
from alt_ani_core.errors import (
    AntiBotError,
    DownloadFailedError,
    FreshResolveLimitError,
    NoStreamError,
    SourcesExhaustedError,
)
from alt_ani_core.events import SourceAttemptFailed
from alt_ani_core.models import FallbackPolicy
from alt_ani_core.testing import make_stream

from .support import (
    EMBED,
    EP1,
    EP2,
    PLAYER,
    PLAYER2,
    SERIES_REF,
    STREAM,
    STREAM_B,
    ResolveCall,
    http,
    in_turn,
    make_state,
    resolved_for,
)

_PROG = CONTENT["progress"]
_EXTRACT = "alt_ani_core.services.episode_session.extract.resolve"
_QUALITY_STREAM = make_stream(
    "https://cdn.example.com/v.m3u8", qualities={"720p": "https://cdn.example.com/720.m3u8"}, ext="m3u8"
)
_EXHAUSTED = SourcesExhaustedError("nothing worked")


def _resolve_state(players=(PLAYER, PLAYER2), **fields):
    defaults = dict(ref=SERIES_REF, targets=[EP1, EP2], ep_idx=0, players=list(players), chosen_player=players[0])
    return make_state(**(defaults | fields))


@pytest.mark.unit
class TestHandleResolveStream:
    def test_resolves_only_the_chosen_source(self):
        state = _resolve_state(options=CliOptions(cookies_file="c.txt", cookies_browser="firefox"))
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        assert state.session.resolve_calls == [
            ResolveCall(
                chosen=PLAYER,
                candidates=(),
                policy=FallbackPolicy.SINGLE,
                options=ExtractOptions(cookies_file="c.txt", cookies_browser="firefox"),
                force_fresh=False,
            )
        ]
        assert state.resolved == resolved_for(PLAYER)

    def test_labelled_stream_asks_for_quality(self):
        state = _resolve_state()
        state.session.resolve_script = _QUALITY_STREAM
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.QUALITY_PICK
        assert state.resolved.stream is _QUALITY_STREAM

    def test_cached_quality_skips_the_quality_menu(self):
        state = _resolve_state(quality="720p")
        state.session.resolve_script = _QUALITY_STREAM
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK

    def test_exhausted_source_is_marked_failed_and_another_is_offered(self):
        state = _resolve_state()
        state.session.resolve_script = _EXHAUSTED
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.PLAYER_PICK
        assert state.failed_ids == {PLAYER.online_id}
        assert state.ep_idx == 0
        assert state.resolved is None
        mock_warn.assert_not_called()

    def test_last_source_exhausted_skips_the_episode(self):
        state = _resolve_state(chosen_player=PLAYER2, failed_ids={PLAYER.online_id})
        state.session.resolve_script = _EXHAUSTED
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1
        mock_warn.assert_called_once_with(_PROG["no_player_worked"].format(number=1.0))

    def test_without_episode_or_player_is_a_programming_error(self):
        with pytest.raises(AssertionError):
            HANDLERS[Screen.RESOLVE_STREAM](_resolve_state(chosen_player=None))
        with pytest.raises(AssertionError):
            HANDLERS[Screen.RESOLVE_STREAM](make_state(chosen_player=PLAYER))


@pytest.mark.unit
class TestResolveExpiredSource:
    """A source whose stream resolved but then failed to download gets a fresh embed when picked again."""

    def test_download_failure_marks_the_source_expired(self):
        state = _resolve_state(resolved=resolved_for(PLAYER), episode_action="download")
        with (
            patch("alt_ani_cli.flow.handlers.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            assert HANDLERS[Screen.RUN_ACTION](state) is Screen.PLAYER_PICK
        assert state.expired_ids == {PLAYER.online_id}

    def test_picking_an_expired_source_forces_a_fresh_resolve(self):
        state = _resolve_state(expired_ids={PLAYER.online_id})
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        assert [(c.chosen, c.policy, c.force_fresh) for c in state.session.resolve_calls] == [
            (PLAYER, FallbackPolicy.SINGLE, True)
        ]

    def test_other_sources_are_resolved_normally(self):
        state = _resolve_state(chosen_player=PLAYER2, expired_ids={PLAYER.online_id})
        HANDLERS[Screen.RESOLVE_STREAM](state)
        assert [c.force_fresh for c in state.session.resolve_calls] == [False]

    def test_fresh_resolve_limit_falls_back_to_a_normal_resolve(self):
        state = _resolve_state(expired_ids={PLAYER.online_id})
        state.session.resolve_script = [FreshResolveLimitError("limit"), STREAM_B]
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        assert [c.force_fresh for c in state.session.resolve_calls] == [True, False]
        assert state.resolved.stream is STREAM_B

    def test_exhausted_fresh_resolve_fails_the_source_without_a_second_try(self):
        state = _resolve_state(expired_ids={PLAYER.online_id})
        state.session.resolve_script = [_EXHAUSTED]
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.PLAYER_PICK
        assert len(state.session.resolve_calls) == 1
        assert state.failed_ids == {PLAYER.online_id}

    def test_dispatch_forgets_expired_sources(self):
        state = _resolve_state(expired_ids={PLAYER.online_id})
        state.engine.open_episode.side_effect = None
        state.engine.open_episode.return_value = state.session
        state.session.sources = (PLAYER, PLAYER2)
        assert HANDLERS[Screen.EPISODE_DISPATCH](state) is Screen.PLAYER_PICK
        assert state.expired_ids == set()

    def test_repick_after_download_failure_requests_a_new_embed(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER, episode_action="download")
        shinden.player(
            PLAYER, show=in_turn('<iframe src="https://host.example/e/old">', '<iframe src="https://host.example/e/new">')
        )
        with (
            patch(_EXTRACT, return_value=STREAM) as mock_extract,
            patch("alt_ani_cli.flow.handlers.download.run", side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
            assert HANDLERS[Screen.RUN_ACTION](state) is Screen.PLAYER_PICK
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id] * 2
        assert [c.args[0] for c in mock_extract.call_args_list] == ["https://host.example/e/old", "https://host.example/e/new"]
        assert state.resolved.embed.url == "https://host.example/e/new"


@pytest.mark.unit
class TestResolveStreamEmbedCache:
    def _primed_state(self, open_state, shinden, show):
        state = open_state(players=(PLAYER,), chosen_player=PLAYER)
        shinden.player(PLAYER, show=show)
        assert state.session.probe_hosts([PLAYER]) is None
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id]
        return state

    def test_cached_embed_skips_resolve(self, open_state, shinden):
        state = self._primed_state(open_state, shinden, f'<iframe src="{EMBED.url}">')
        with patch(_EXTRACT, return_value=STREAM) as mock_extract:
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id]
        assert mock_extract.call_args[0][0] == EMBED.url
        assert result is Screen.ACTION_PICK
        assert state.resolved.stream is STREAM
        assert state.resolved.embed == EMBED

    def test_repicking_a_resolved_player_requests_player_load_once(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER)
        with (
            patch(_EXTRACT, return_value=STREAM) as mock_extract,
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", PLAYER)),
        ):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.RESOLVE_STREAM
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id]
        assert shinden.xhr_ids("player_show") == [PLAYER.online_id]
        assert mock_extract.call_count == 2

    def test_expired_cached_embed_falls_back_to_fresh_resolve(self, open_state, shinden):
        fresh_url = "https://kerapoxy.cc/e/new"
        show = in_turn(f'<iframe src="{EMBED.url}">', f'<iframe src="{fresh_url}">')
        state = self._primed_state(open_state, shinden, show)
        with patch(_EXTRACT, side_effect=[NoStreamError("expired"), STREAM]) as mock_extract:
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id] * 2
        assert mock_extract.call_count == 2
        assert mock_extract.call_args[0][0] == fresh_url
        assert result is Screen.ACTION_PICK
        assert state.resolved.embed.url == fresh_url


@pytest.mark.unit
class TestResolveStreamFailureRecordsHost:
    """A player whose embed resolved but whose extraction failed should still surface its host."""

    def test_extraction_failure_records_host_from_resolved_embed(self, open_state, shinden, sink):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER, embed="https://playmate.to/e/xyz")
        with (
            patch(_EXTRACT, side_effect=NoStreamError("dead")),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert result is Screen.PLAYER_PICK
        assert PLAYER.online_id in state.failed_ids
        source = state.session.known_sources[PLAYER.online_id]
        assert (source.host, source.embed_url) == ("playmate.to", "https://playmate.to/e/xyz")
        assert [event.player for event in sink.of(SourceAttemptFailed)] == [PLAYER.player]

    def test_antibot_error_leaves_host_unknown(self, open_state):
        state = open_state(chosen_player=PLAYER)
        with (
            patch("alt_ani_core.shinden.api.resolve_embed", side_effect=AntiBotError("blocked")),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert result is Screen.PLAYER_PICK
        assert PLAYER.online_id in state.failed_ids
        assert PLAYER.online_id not in state.session.known_sources


@pytest.mark.unit
class TestResolveStreamNeverReorders:
    """order() is an auto-mode, noninteractive-only concern — the interactive picker must never trigger it."""

    def test_health_order_is_never_called(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER2)
        shinden.player(PLAYER2)
        with (
            patch(_EXTRACT, side_effect=NoStreamError("dead")),
            patch("alt_ani_core.rules.health.ResolverHealth.order") as mock_order,
        ):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.PLAYER_PICK

        mock_order.assert_not_called()
        assert shinden.xhr_ids("player_load") == [PLAYER2.online_id]

    def test_known_bad_host_is_still_tried_when_picked(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER)
        with (
            patch("alt_ani_core.rules.health.ResolverHealth.should_defer", return_value=True),
            patch(_EXTRACT, return_value=STREAM) as mock_extract,
        ):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        mock_extract.assert_called_once()


@pytest.mark.unit
class TestResolveStreamXhrStop:
    @pytest.mark.parametrize(
        "load",
        [http(429, headers={"Retry-After": "60"}), http(200, '{"error": {"code": "turnstile"}}'), http(503)],
        ids=["429", "turnstile", "503"],
    )
    def test_stop_returns_to_player_pick_without_marking_player_failed(self, open_state, shinden, sink, load):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER, load=load)
        with (
            patch("alt_ani_cli.ui.progress.warn"),
            patch("alt_ani_cli.ui.progress.error") as mock_error,
        ):
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert result is Screen.PLAYER_PICK
        assert state.failed_ids == set()
        assert state.resolved is None
        assert state.reload_pending is False
        mock_error.assert_called_once()
        assert sink.of(SourceAttemptFailed) == []
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id]

    def test_block_message_shows_retry_after(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER, load=http(429, headers={"Retry-After": "60"}))
        with patch("alt_ani_cli.ui.progress.error") as mock_error:
            HANDLERS[Screen.RESOLVE_STREAM](state)
        assert "60 s" in mock_error.call_args.args[0]
        assert "auth=" not in mock_error.call_args.args[0]

    def test_block_is_not_persisted_and_manual_retry_resolves_again(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER, load=in_turn(http(429), "0"))
        with (
            patch(_EXTRACT, return_value=STREAM),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.PLAYER_PICK
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        assert shinden.xhr_ids("player_load") == [PLAYER.online_id] * 2
        assert state.resolved.stream is STREAM

    def test_player_local_xhr_failure_keeps_existing_failed_player_flow(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER, load=http(404))
        with patch("alt_ani_cli.ui.progress.error") as mock_error:
            result = HANDLERS[Screen.RESOLVE_STREAM](state)
        assert result is Screen.PLAYER_PICK
        assert state.failed_ids == {PLAYER.online_id}
        mock_error.assert_not_called()

    def test_interactive_session_survives_a_block(self, open_state, shinden):
        state = open_state(chosen_player=PLAYER)
        shinden.player(PLAYER, load=http(403))
        with (
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)) as mock_pick,
        ):
            assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.PLAYER_PICK
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.EPISODES_PICK
        # the picker must not claim the chosen player itself failed
        mock_warn.assert_not_called()
        assert mock_pick.call_args.kwargs["failed"] == set()


@pytest.mark.unit
class TestHandleQualityPick:
    def test_esc_returns_player_pick_and_resets_quality(self):
        state = make_state(resolved=resolved_for(PLAYER, _QUALITY_STREAM), quality="1080p")
        with patch("alt_ani_cli.ui.menus.select_quality", return_value=None):
            result = HANDLERS[Screen.QUALITY_PICK](state)
        assert result is Screen.PLAYER_PICK
        assert state.quality is None

    def test_pick_caches_quality(self):
        state = make_state(resolved=resolved_for(PLAYER, _QUALITY_STREAM))
        with patch("alt_ani_cli.ui.menus.select_quality", return_value="720p") as mock_quality:
            result = HANDLERS[Screen.QUALITY_PICK](state)
        mock_quality.assert_called_once_with(("720p",))
        assert result is Screen.ACTION_PICK
        assert state.quality == "720p"


@pytest.mark.unit
class TestHandleActionPick:
    def test_esc_resets_action_and_returns_player_pick(self):
        state = make_state(resolved=resolved_for(PLAYER, STREAM))
        with patch("alt_ani_cli.ui.menus.select_action", return_value=None):
            result = HANDLERS[Screen.ACTION_PICK](state)
        assert result is Screen.PLAYER_PICK
        assert state.episode_action is None

    def test_esc_returns_quality_pick_when_qualities_present(self):
        state = make_state(resolved=resolved_for(PLAYER, _QUALITY_STREAM))
        with patch("alt_ani_cli.ui.menus.select_action", return_value=None):
            result = HANDLERS[Screen.ACTION_PICK](state)
        assert result is Screen.QUALITY_PICK

    @pytest.mark.parametrize("action", ["play", "download", "debug"])
    def test_picked_action_is_cached(self, action):
        state = make_state(resolved=resolved_for(PLAYER, STREAM))
        with patch("alt_ani_cli.ui.menus.select_action", return_value=action):
            assert HANDLERS[Screen.ACTION_PICK](state) is Screen.RUN_ACTION
        assert state.episode_action == action

    def test_cached_action_skips_menu(self):
        state = make_state(episode_action="play")
        with patch("alt_ani_cli.ui.menus.select_action") as mock_act:
            result = HANDLERS[Screen.ACTION_PICK](state)
        mock_act.assert_not_called()
        assert result is Screen.RUN_ACTION

    def test_args_download_skips_menu(self):
        state = make_state(options=CliOptions(download=True))
        with patch("alt_ani_cli.ui.menus.select_action") as mock_act:
            result = HANDLERS[Screen.ACTION_PICK](state)
        mock_act.assert_not_called()
        assert result is Screen.RUN_ACTION
        assert state.episode_action == "download"

    def test_args_debug_skips_menu(self):
        state = make_state(options=CliOptions(debug=True))
        with patch("alt_ani_cli.ui.menus.select_action") as mock_act:
            result = HANDLERS[Screen.ACTION_PICK](state)
        mock_act.assert_not_called()
        assert result is Screen.RUN_ACTION
        assert state.episode_action == "debug"
