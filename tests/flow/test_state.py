"""FlowState holds navigation only; resolver health lives on the engine for the whole interactive session."""

import dataclasses
import json
from unittest.mock import patch

import pytest

from alt_ani_cli.args import CliOptions
from alt_ani_cli.flow.handlers import handle_episode_dispatch, handle_resolve_stream
from alt_ani_cli.flow.state import BACK, FlowState, Screen
from alt_ani_core.errors import NoStreamError
from alt_ani_core.models import EpisodeRow, ExtractionFailure, PlayerEntry, SeriesRef
from alt_ani_core.rules.health import HostState, ResolverHealth, Signal
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.testing import make_stream

_SERIES_REF = SeriesRef(id="1", slug="fate", title="Fate", url="https://shinden.pl/series/1-fate")
_EP1 = EpisodeRow(number=1.0, title="Ep 1", url="https://shinden.pl/episode/1-fate/view/101")
_EP2 = EpisodeRow(number=2.0, title="Ep 2", url="https://shinden.pl/episode/1-fate/view/102")
_PLAYER = PlayerEntry(online_id="71", player="Sibnet", lang_audio="jp", lang_subs="pl")
_EMBED_HOST = "video.sibnet.ru"


def _episode_page(player: PlayerEntry) -> str:
    """No page token, so with the api4 backend forced the episode opens without a fallback warning."""
    data = {
        "players": [{"online_id": int(player.online_id), "player": player.player, "audio": "jp", "sub": "pl", "res": None}],
        "pulapka": None,
    }
    return (
        f'<html><body><ul class="ep-pl-list"><li class="ep-pl-row" data-online-id="{player.online_id}"></li></ul>'
        f'<script type="application/json" id="episode-players-data">{json.dumps(data)}</script></body></html>'
    )


def _xhr(request) -> str:
    return "0" if "/player_load" in request.url else f'<iframe src="//{_EMBED_HOST}/shell.php?videoid=1"></iframe>'


@pytest.fixture
def engine(make_engine, transport):
    transport.route(_EP1.url, _episode_page(_PLAYER))
    transport.route(_EP2.url, _episode_page(_PLAYER))
    transport.route("https://api4.shinden.pl/xhr/", _xhr)
    return make_engine(xhr_backend="api4")


@pytest.fixture
def state(engine, cli_paths) -> FlowState:
    return FlowState(options=CliOptions(), engine=engine, paths=cli_paths, ref=_SERIES_REF, targets=[_EP1, _EP2])


@pytest.mark.unit
class TestFlowStateFields:
    def test_required_collaborators(self, engine, cli_paths):
        options = CliOptions(url="u")
        state = FlowState(options=options, engine=engine, paths=cli_paths)
        assert state.options is options
        assert state.engine is engine
        assert state.paths is cli_paths

    def test_defaults_describe_a_fresh_flow(self, engine, cli_paths):
        state = FlowState(options=CliOptions(), engine=engine, paths=cli_paths)
        assert state.session is None
        assert state.reload_pending is False
        assert state.resolved is None
        assert state.expired_ids == set()
        assert state.failed_ids == set()
        assert state.players == []
        assert state.chosen_player is None
        assert state.pinned_player is None
        assert state.episodes_complete is True
        assert state.current_ep is None

    def test_episode_state_moved_out_of_the_flow_state(self):
        names = {f.name for f in dataclasses.fields(FlowState)}
        assert {"options", "engine", "paths", "session", "reload_pending", "resolved", "expired_ids"} <= names
        moved = {"args", "client", "episode_xhr", "player_embeds", "player_sources", "players_reloaded_ep", "health"}
        assert not names & moved
        assert not names & {"stream", "embed"}

    def test_mutable_defaults_are_not_shared_between_instances(self, engine, cli_paths):
        state_a = FlowState(options=CliOptions(), engine=engine, paths=cli_paths)
        state_b = FlowState(options=CliOptions(), engine=engine, paths=cli_paths)
        state_a.failed_ids.add("1")
        state_a.expired_ids.add("1")
        state_a.targets.append(_EP1)
        assert state_b.failed_ids == set()
        assert state_b.expired_ids == set()
        assert state_b.targets == []

    def test_current_ep_follows_the_index(self, state):
        assert state.current_ep is _EP1
        state.ep_idx = 1
        assert state.current_ep is _EP2
        state.ep_idx = 2
        assert state.current_ep is None

    def test_back_is_a_distinct_sentinel(self):
        assert BACK is not None
        assert not isinstance(BACK, Screen)


@pytest.mark.unit
class TestFlowStateHealth:
    def test_engine_has_resolver_health_instance(self, state):
        assert isinstance(state.engine.health, ResolverHealth)

    def test_two_engines_do_not_share_store(self, make_engine, cli_paths):
        state_a = FlowState(options=CliOptions(), engine=make_engine(), paths=cli_paths)
        state_b = FlowState(options=CliOptions(), engine=make_engine(), paths=cli_paths)

        state_a.engine.health.record("host.com", "a", Signal.HARD)

        assert state_a.engine.health.state("host.com") == HostState.UNAVAILABLE
        assert state_b.engine.health.state("host.com") == HostState.UNKNOWN

    def test_episode_dispatch_does_not_reset_health(self, state):
        health = state.engine.health
        health.record("host.com", "a", Signal.HARD)

        handle_episode_dispatch(state)

        assert state.engine.health is health
        assert health.state("host.com") == HostState.UNAVAILABLE

    def test_episode_dispatch_opens_a_session_for_the_current_episode(self, state):
        result = handle_episode_dispatch(state)

        assert isinstance(state.session, EpisodeSession)
        assert state.session.episode is _EP1
        assert state.players == [_PLAYER]
        # a single player is picked automatically
        assert state.chosen_player == _PLAYER
        assert result is Screen.RESOLVE_STREAM

    def test_each_dispatch_replaces_the_session_but_keeps_the_health(self, state):
        handle_episode_dispatch(state)
        first = state.session
        state.ep_idx = 1

        handle_episode_dispatch(state)

        assert state.session is not first
        assert state.session.episode is _EP2
        assert state.resolved is None

    def test_resolve_stream_records_its_outcome_on_the_engine_health(self, state):
        handle_episode_dispatch(state)
        failure = NoStreamError("gone")
        failure.failure = ExtractionFailure(layer="custom", category="http_error", http_status=403)

        with patch("alt_ani_core.services.episode_session.extract.resolve", side_effect=failure):
            handle_resolve_stream(state)

        assert state.resolved is None
        assert state.failed_ids == {_PLAYER.online_id}
        assert state.engine.health.state(_EMBED_HOST) == HostState.DEGRADED

    def test_resolve_stream_stores_the_resolved_stream_and_marks_the_host_healthy(self, state):
        handle_episode_dispatch(state)
        stream = make_stream()

        with patch("alt_ani_core.services.episode_session.extract.resolve", return_value=stream):
            result = handle_resolve_stream(state)

        assert result is Screen.ACTION_PICK
        assert state.resolved.stream is stream
        assert state.resolved.source == _PLAYER
        assert state.resolved.embed.url == f"https://{_EMBED_HOST}/shell.php?videoid=1"
        assert state.engine.health.state(_EMBED_HOST) == HostState.HEALTHY
        assert state.session.known_sources[_PLAYER.online_id].host == _EMBED_HOST
