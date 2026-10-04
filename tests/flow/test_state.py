"""Tests for FlowState — ResolverHealth lives for the whole interactive session."""

import argparse
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.flow.handlers import handle_episode_dispatch, handle_resolve_stream
from alt_ani_cli.flow.state import FlowState
from alt_ani_cli.health import HostState, ResolverHealth, Signal
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, SeriesRef

_SERIES_REF = SeriesRef(id="1", slug="fate", title="Fate", url="http://shinden.pl/series/1-fate")
_EP1 = EpisodeRow(number=1.0, title="Ep 1", url="http://shinden.pl/ep/1")
_PLAYER = PlayerEntry(online_id="pid1", player="Sibnet", lang_audio="jp", lang_subs="pl")


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


@pytest.mark.unit
class TestFlowStateHealth:
    def test_has_resolver_health_instance(self):
        state = FlowState(args=_make_args(), client=MagicMock())
        assert isinstance(state.health, ResolverHealth)

    def test_two_instances_do_not_share_store(self):
        state_a = FlowState(args=_make_args(), client=MagicMock())
        state_b = FlowState(args=_make_args(), client=MagicMock())

        state_a.health.record("host.com", "a", Signal.HARD)

        assert state_a.health.state("host.com") == HostState.UNAVAILABLE
        assert state_b.health.state("host.com") == HostState.UNKNOWN

    def test_episode_dispatch_does_not_reset_health(self):
        state = FlowState(args=_make_args(), client=MagicMock(), ref=_SERIES_REF, targets=[_EP1], ep_idx=0)
        state.health.record("host.com", "a", Signal.HARD)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = ""
        state.client.get.return_value = mock_resp

        with (
            patch("alt_ani_cli.shinden.episode.parse_players", return_value=[_PLAYER]),
            patch("alt_ani_cli.shinden.episode.sort_players", return_value=[_PLAYER]),
            patch("alt_ani_cli.diagnostics.player_selected"),
        ):
            handle_episode_dispatch(state)

        assert state.health.state("host.com") == HostState.UNAVAILABLE

    def test_resolve_stream_passes_state_health_to_fallback(self):
        state = FlowState(
            args=_make_args(),
            client=MagicMock(),
            ref=_SERIES_REF,
            targets=[_EP1],
            ep_idx=0,
            players=[_PLAYER],
            chosen_player=_PLAYER,
        )
        with patch("alt_ani_cli.cli._resolve_with_fallback", return_value=(None, None)) as mock_fallback:
            handle_resolve_stream(state)

        assert mock_fallback.call_args.kwargs["health"] is state.health
