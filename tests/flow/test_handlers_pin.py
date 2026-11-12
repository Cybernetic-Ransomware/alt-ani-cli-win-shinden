from unittest.mock import patch

import pytest

from alt_ani_cli.args import CliOptions
from alt_ani_cli.content import CONTENT
from alt_ani_cli.flow.handlers import HANDLERS, handle_run_action
from alt_ani_cli.flow.state import FlowState, Screen
from alt_ani_core.errors import DownloadTargetError, SourcesExhaustedError
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.models import EpisodeListing, PlayerEntry
from alt_ani_core.rules.pin import player_fingerprint
from alt_ani_core.testing import make_stream

from .support import (
    EP1,
    EP4,
    EP5,
    EP6,
    SERIES_REF,
    STREAM,
    FakeDownloads,
    download_state,
    fake_engine,
    make_state,
    resolved_for,
    run_download,
    serve_players,
)

_PIN_A1 = PlayerEntry(
    online_id="201",
    player="CDA",
    lang_audio="jp",
    lang_subs="pl",
    subs_author="Mioro",
    source="https://www.miorosubs.com/?ep=4",
)
_PIN_A2 = PlayerEntry(
    online_id="202", player="CDA", lang_audio="jp", lang_subs="pl", subs_author="Mioro", source="http://miorosubs.com/?ep=5"
)
_PIN_A3 = PlayerEntry(
    online_id="203", player="CDA", lang_audio="jp", lang_subs="pl", subs_author="Mioro", source="https://miorosubs.com/x"
)
_OTHER = PlayerEntry(online_id="301", player="Sibnet", lang_audio="jp", lang_subs="pl", source="https://anisubs.pl/")
_OTHER_EN = PlayerEntry(online_id="302", player="CDA", lang_audio="jp", lang_subs="en")
_PIN_FP = player_fingerprint(_PIN_A1)
_NO_MATCH = CONTENT["progress"]["pin_no_match"].format(number=5.0)


def _pin_batch_state(**fields) -> FlowState:
    defaults = dict(
        targets=[EP4, EP5, EP6],
        ep_idx=1,
        players=[_PIN_A1, _OTHER],
        chosen_player=_PIN_A1,
        episode_action="download",
        pinned_player=_PIN_FP,
    )
    return download_state(**(defaults | fields))


def _pin_dispatch(players, *, state=None, confirm=True, **option_fields):
    """Run EPISODE_DISPATCH with ranking mocked: sort_players returns ``players`` as the final order."""
    if state is None:
        state = _pin_batch_state(options=CliOptions(**option_fields))
    serve_players(state.engine, players)
    state.engine.open_episode.reset_mock()
    with (
        patch("alt_ani_core.rules.ranking.sort_players", return_value=list(players)) as mock_sort,
        patch("alt_ani_core.diagnostics.player_selected") as mock_diag,
        patch("alt_ani_cli.ui.menus.confirm", return_value=confirm),
        patch("alt_ani_cli.ui.progress.info") as mock_info,
        patch("alt_ani_cli.ui.progress.warn"),
    ):
        result = HANDLERS[Screen.EPISODE_DISPATCH](state)
    return state, result, {"sort": mock_sort, "diag": mock_diag, "info": mock_info}


def _manual_fallback(picked, *, state=None, pin_choice=None, qualities=None):
    """PLAYER_PICK (manual pick) → RESOLVE_STREAM → ACTION_PICK with download cached and pin active."""
    if state is None:
        state = _pin_batch_state(players=[_PIN_A2, _OTHER, _PIN_A3], chosen_player=None)
    state.session.resolve_script = make_stream(STREAM.default.url, qualities=qualities, ext="m3u8")
    with (
        patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", picked)),
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
class TestPinFirstSelection:
    def test_download_and_pin_sets_pin_and_downloads_current_episode(self):
        state = _pin_batch_state(ep_idx=0, episode_action=None, pinned_player=None)
        with patch("alt_ani_cli.ui.menus.select_action", return_value="download_pin") as mock_action:
            assert HANDLERS[Screen.ACTION_PICK](state) is Screen.RUN_ACTION
        assert mock_action.call_args.kwargs == {"offer_pin": True}
        assert state.episode_action == "download"
        assert state.pinned_player == _PIN_FP

        fake = FakeDownloads()
        result, _ = run_download(state, fake)
        assert fake.outcomes == [("downloaded", 4.0)]
        assert state.engine.history.download_calls == [(SERIES_REF, 4.0)]
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)
        assert state.pinned_player == _PIN_FP

    @pytest.mark.parametrize(("targets", "ep_idx"), [([EP4], 0), ([EP4, EP5], 1)])
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
        state, _, _ = _pin_dispatch([_OTHER, _PIN_A2], show_sources=True)
        state.engine.open_episode.assert_called_once_with(EP5)
        assert state.session.probe_calls == []
        assert state.session.resolve_calls == []
        assert state.session.known_sources == {}

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
        other_sibnet = PlayerEntry(online_id="303", player="Sibnet", lang_audio="jp", lang_subs="en")
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
        mocks["info"].assert_called_with(_NO_MATCH)

    def test_without_pin_dispatch_is_unchanged(self):
        state, result, mocks = _pin_dispatch([_OTHER, _PIN_A2], state=_pin_batch_state(pinned_player=None))
        assert result is Screen.PLAYER_PICK
        mocks["diag"].assert_not_called()
        assert all(c.args[0] != _NO_MATCH for c in mocks["info"].call_args_list)

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
        state.session.resolve_script = SourcesExhaustedError("nothing worked")
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
        result, _ = run_download(state, FakeDownloads(fail_urls={STREAM.default.url}))
        assert result is Screen.PLAYER_PICK
        assert state.failed_ids == {_PIN_A2.online_id}
        assert state.ep_idx == 1
        assert state.pinned_player == _PIN_FP
        assert state.engine.history.download_calls == []

    def test_download_target_error_keeps_pin_and_player(self):
        state = _pin_batch_state(players=[_OTHER, _PIN_A2], chosen_player=_PIN_A2)
        with (
            patch("alt_ani_cli.flow.handlers.download.run", side_effect=DownloadTargetError("locked")),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            assert handle_run_action(state) is Screen.PLAYER_PICK
        assert state.failed_ids == set()
        assert state.ep_idx == 1
        assert state.pinned_player == _PIN_FP
        assert state.engine.history.download_calls == []


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
        _, result, _ = _manual_fallback(_OTHER, pin_choice=None, qualities={"720p": "https://cdn.example.com/720.m3u8"})
        assert result is Screen.QUALITY_PICK

    def test_auto_picked_player_with_other_fingerprint_does_not_ask(self):
        state, _, _ = _pin_dispatch([_OTHER, _OTHER_EN], select_nth=1)
        state.resolved = resolved_for(state.chosen_player)
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
        state = make_state(ref=SERIES_REF, episodes=[EP4, EP5], pinned_player=_PIN_FP)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP5]):
            HANDLERS[Screen.EPISODES_PICK](state)
        assert state.pinned_player is None

    def test_cli_episode_range_batch_clears_pin(self):
        state = make_state(ref=SERIES_REF, episodes=[EP4, EP5], pinned_player=_PIN_FP, options=CliOptions(episode="4-5"))
        HANDLERS[Screen.EPISODES_PICK](state)
        assert state.pinned_player is None

    def test_cancel_batch_clears_pin(self):
        state = _pin_batch_state(chosen_player=_PIN_A2)
        result, _ = run_download(state, FakeDownloads(existing={5.0}), "cancel")
        assert result is Screen.EPISODES_PICK
        assert state.pinned_player is None

    def test_esc_from_player_pick_clears_pin(self):
        state = _pin_batch_state()
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)):
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.EPISODES_PICK
        assert state.pinned_player is None

    def test_new_series_clears_pin(self):
        state = make_state(ref=SERIES_REF, pinned_player=_PIN_FP)
        state.engine.episodes.return_value = EpisodeListing(SERIES_REF, [EP1])
        HANDLERS[Screen.FETCH_EPISODES](state)
        assert state.pinned_player is None

    @pytest.mark.parametrize("answer", [False, None])
    def test_filter_mismatch_back_to_episode_pick_clears_pin(self, answer):
        state = _pin_batch_state(options=CliOptions(lang="xx"))
        state, result, _ = _pin_dispatch([_OTHER, _PIN_A2], state=state, confirm=answer)
        assert result is Screen.EPISODES_PICK
        assert state.pinned_player is None

    def test_quality_pick_keeps_pin(self):
        stream = make_stream(STREAM.default.url, qualities={"720p": "https://cdn.example.com/720.m3u8"}, ext="m3u8")
        state = _pin_batch_state(resolved=resolved_for(_PIN_A1, stream))
        with patch("alt_ani_cli.ui.menus.select_quality", return_value="720p"):
            HANDLERS[Screen.QUALITY_PICK](state)
        assert state.pinned_player == _PIN_FP

    def test_next_episode_in_batch_keeps_pin(self):
        state = _pin_batch_state(chosen_player=_PIN_A2)
        result, _ = run_download(state, FakeDownloads())
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 2)
        assert state.pinned_player == _PIN_FP


@pytest.mark.unit
class TestPinIndependentOfOverwritePolicy:
    def test_overwrite_remaining_does_not_pin(self):
        state = _pin_batch_state(pinned_player=None, chosen_player=_PIN_A2)
        run_download(state, FakeDownloads(existing={5.0}), "overwrite_remaining")
        assert state.overwrite_existing_batch is True
        assert state.pinned_player is None

    def test_policy_and_pin_both_survive_pinned_download_failure(self):
        state = _pin_batch_state(players=[_OTHER, _PIN_A2], chosen_player=_PIN_A2, overwrite_existing_batch=True)
        run_download(state, FakeDownloads(existing={5.0}, fail_urls={STREAM.default.url}))
        assert state.overwrite_existing_batch is True
        assert state.pinned_player == _PIN_FP

    def test_repin_keeps_overwrite_policy(self):
        state = _pin_batch_state(players=[_PIN_A2, _OTHER], chosen_player=None, overwrite_existing_batch=True)
        state, _, _ = _manual_fallback(_OTHER, state=state, pin_choice="repin")
        assert state.overwrite_existing_batch is True


@pytest.mark.unit
class TestPinNotPersisted:
    def test_history_database_has_no_pin_data(self, cli_paths):
        history = SqliteHistory(cli_paths.history_db)
        state = _pin_batch_state(chosen_player=_PIN_A2, engine=fake_engine(history=history))
        run_download(state, FakeDownloads())
        assert [(e.series, e.episodes) for e in history.download_entries()] == [(SERIES_REF, frozenset({5.0}))]
        stored = b"".join(path.read_bytes() for path in sorted(cli_paths.state_dir.iterdir()) if path.is_file())
        assert b"Fate" in stored
        assert b"miorosubs" not in stored
        assert b"pinned" not in stored.lower()
