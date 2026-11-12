from unittest.mock import patch

import pytest

from alt_ani_cli.args import CliOptions
from alt_ani_cli.content import CONTENT, EXCEPTIONS_PL
from alt_ani_cli.flow.handlers import HANDLERS, handle_run_action
from alt_ani_cli.flow.state import Screen
from alt_ani_cli.player.runner import PlayResult
from alt_ani_core.errors import DownloadFailedError, DownloadTargetError
from alt_ani_core.models import EpisodeListing
from alt_ani_core.testing import make_stream

from .support import (
    EMBED,
    EP1,
    EP2,
    EP4,
    EP5,
    EP6,
    EP7,
    PLAYER,
    PLAYER2,
    SERIES_REF,
    STREAM,
    STREAM_B,
    FakeDownloads,
    batch_state,
    download_state,
    make_state,
    resolved_for,
    run_download,
)

_PROG = CONTENT["progress"]
_DOWNLOAD_RUN = "alt_ani_cli.flow.handlers.download.run"
_QUALITY_STREAM = make_stream(
    "https://cdn.example.com/v.m3u8",
    qualities={"480p": "https://cdn.example.com/480.m3u8", "1080p": "https://cdn.example.com/1080.m3u8"},
    ext="m3u8",
)


def _play_state(episode_action="play", **option_fields):
    return make_state(
        options=CliOptions(**option_fields),
        ref=SERIES_REF,
        targets=[EP1],
        ep_idx=0,
        resolved=resolved_for(PLAYER, STREAM, EMBED),
        episode_action=episode_action,
        players=[PLAYER],
        chosen_player=PLAYER,
    )


@pytest.mark.unit
class TestHandleRunActionPlaybackReporting:
    """no_detach's return code must actually be surfaced — see mpv.py --no-detach diagnostics."""

    def test_no_detach_nonzero_rc_reports_error_not_success(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=5.0)),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_error.assert_called_once()
        mock_success.assert_not_called()
        mock_warn.assert_not_called()

    def test_no_detach_zero_rc_slow_exit_reports_success(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_success.assert_called_once()
        mock_error.assert_not_called()
        mock_warn.assert_not_called()

    def test_no_detach_zero_rc_fast_exit_reports_unconfirmed_not_success(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.1)),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_warn.assert_called_once()
        mock_success.assert_not_called()
        mock_error.assert_not_called()

    def test_detached_mode_ignores_rc_and_elapsed_reports_success(self):
        state = _play_state(no_detach=False)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=0.0)),
            patch("alt_ani_cli.ui.progress.success") as mock_success,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_success.assert_called_once()
        mock_error.assert_not_called()
        mock_warn.assert_not_called()

    def test_no_detach_mpv_reports_log_file_hint(self):
        state = _play_state(no_detach=True, vlc=False)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info") as mock_info,
        ):
            handle_run_action(state)
        mock_info.assert_called_once()
        assert str(state.paths.mpv_log) in mock_info.call_args[0][0]
        assert "mpv-debug.log" in mock_info.call_args[0][0]

    def test_no_detach_vlc_does_not_report_mpv_log_hint(self):
        state = _play_state(no_detach=True, vlc=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info") as mock_info,
        ):
            handle_run_action(state)
        mock_info.assert_not_called()

    def test_detached_mode_also_reports_log_file_hint(self):
        state = _play_state(no_detach=False, vlc=False)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.0)),
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info") as mock_info,
        ):
            handle_run_action(state)
        mock_info.assert_called_once()
        assert "mpv-debug.log" in mock_info.call_args[0][0]

    @pytest.mark.parametrize(("vlc", "kind"), [(False, "mpv"), (True, "vlc")])
    def test_player_gets_the_chosen_variant_and_title(self, vlc, kind):
        state = _play_state(no_detach=True, vlc=vlc)
        with (
            patch("alt_ani_cli.flow.handlers.player_runner.play", return_value=PlayResult(rc=0, elapsed=5.0)) as mock_play,
            patch("alt_ani_cli.flow.handlers.playback.report_playback") as mock_report,
        ):
            handle_run_action(state)
        mock_play.assert_called_once_with(STREAM.default, kind=kind, title="Fate — Odcinek 1", no_detach=True)
        mock_report.assert_called_once_with(PlayResult(rc=0, elapsed=5.0), state.options, "Fate — Odcinek 1")

    @pytest.mark.parametrize(
        ("quality", "url"),
        [(None, "https://cdn.example.com/1080.m3u8"), ("480p", "https://cdn.example.com/480.m3u8")],
    )
    def test_quality_choice_selects_the_variant(self, quality, url):
        state = _play_state()
        state.resolved = resolved_for(PLAYER, _QUALITY_STREAM)
        state.quality = quality
        with (
            patch("alt_ani_cli.flow.handlers.player_runner.play", return_value=PlayResult(rc=0, elapsed=0.0)) as mock_play,
            patch("alt_ani_cli.flow.handlers.playback.report_playback"),
        ):
            handle_run_action(state)
        assert mock_play.call_args.args[0].url == url


@pytest.mark.unit
class TestHandleRunActionHistoryTracking:
    """History/completed_eps must only record playback actually confirmed as watched."""

    def _watched(self, state):
        return [(entry.series, entry.last_ep) for entry in state.engine.history.watch_entries()]

    def test_confirmed_playback_updates_history_and_completed_eps(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.ui.progress.success"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        assert state.engine.history.watch_calls == [(SERIES_REF, EP1.number)]
        assert self._watched(state) == [(SERIES_REF, EP1.number)]
        assert EP1.number in state.completed_eps

    def test_unconfirmed_fast_exit_does_not_update_history(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.1)),
            patch("alt_ani_cli.ui.progress.warn"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        assert state.engine.history.watch_calls == []
        assert state.completed_eps == set()

    def test_failed_playback_does_not_update_history(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=5.0)),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
        assert state.engine.history.watch_calls == []
        assert state.completed_eps == set()

    def test_detached_mode_updates_history_unconditionally(self):
        state = _play_state(no_detach=False)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=0.0)),
            patch("alt_ani_cli.ui.progress.success"),
        ):
            handle_run_action(state)
        assert state.engine.history.watch_calls == [(SERIES_REF, EP1.number)]
        assert EP1.number in state.completed_eps

    def test_history_follows_the_playback_verdict(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=0, elapsed=5.0)),
            patch("alt_ani_cli.flow.handlers.playback.report_playback"),
            patch("alt_ani_cli.flow.handlers.playback.playback_confirmed", return_value=False) as mock_confirmed,
        ):
            handle_run_action(state)
        mock_confirmed.assert_called_once_with(PlayResult(rc=0, elapsed=5.0), True)
        assert state.engine.history.watch_calls == []

    def test_debug_action_does_not_update_history(self):
        # Showing the debug link table is not the same as watching the episode.
        state = _play_state(episode_action="debug")
        with (
            patch("alt_ani_cli.flow.handlers.playback.print_debug") as mock_debug,
            patch("alt_ani_cli.player.runner.play") as mock_play,
        ):
            assert handle_run_action(state) is Screen.EPISODE_DISPATCH
        mock_debug.assert_called_once_with(STREAM, STREAM.default, EMBED)
        mock_play.assert_not_called()
        assert state.engine.history.watch_calls == []
        assert state.completed_eps == set()

    def test_debug_flag_prints_without_playing(self):
        state = _play_state(episode_action=None, debug=True)
        with (
            patch("alt_ani_cli.flow.handlers.playback.print_debug") as mock_debug,
            patch("alt_ani_cli.player.runner.play") as mock_play,
        ):
            handle_run_action(state)
        mock_debug.assert_called_once()
        mock_play.assert_not_called()

    def test_download_action_does_not_update_history(self):
        state = _play_state(episode_action="download")
        with (
            patch(_DOWNLOAD_RUN) as mock_download,
            patch("alt_ani_cli.player.runner.play") as mock_play,
        ):
            handle_run_action(state)
        mock_download.assert_called_once()
        mock_play.assert_not_called()
        assert state.engine.history.watch_calls == []
        assert state.completed_eps == set()

    def test_ep_idx_advances_regardless_of_confirmation(self):
        state = _play_state(no_detach=True)
        with (
            patch("alt_ani_cli.player.runner.play", return_value=PlayResult(rc=1, elapsed=5.0)),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            assert handle_run_action(state) is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1
        assert state.resolved is None


@pytest.mark.unit
class TestHandleRunActionDownloadFailure:
    def test_success_advances_episode_without_history(self):
        state = download_state()
        with (
            patch(_DOWNLOAD_RUN) as mock_download,
            patch("alt_ani_core.diagnostics.download_result") as mock_diag,
        ):
            result = handle_run_action(state)
        mock_download.assert_called_once()
        assert state.engine.history.watch_calls == []
        mock_diag.assert_called_once_with("filemoon.sx", ok=True, exc=None)
        assert result is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1
        assert state.failed_ids == set()
        assert state.expired_ids == set()

    def test_download_gets_variant_destination_and_event_sink(self):
        state = download_state(resolved=resolved_for(PLAYER, _QUALITY_STREAM), quality="480p")
        fake = FakeDownloads()
        run_download(state, fake)
        assert fake.calls == [
            {
                "variant": _QUALITY_STREAM.select("480p"),
                "ep": EP1,
                "ref": SERIES_REF,
                "dest_dir": state.paths.downloads_dir,
                "events": state.engine.events,
            }
        ]

    def test_failure_with_other_players_returns_to_player_pick(self):
        state = download_state()
        with (
            patch(_DOWNLOAD_RUN, side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error") as mock_error,
        ):
            result = handle_run_action(state)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0
        assert state.failed_ids == {PLAYER.online_id}
        assert state.expired_ids == {PLAYER.online_id}
        assert state.resolved is None
        assert state.completed_eps == set()
        assert state.episode_action == "download"
        assert state.engine.history.watch_calls == []
        mock_error.assert_called_once_with(_PROG["download_failed"].format(player=repr(PLAYER.player)))

    def test_failure_logs_redacted_diagnostics(self):
        state = download_state()
        with (
            patch(_DOWNLOAD_RUN, side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_core.diagnostics.download_result") as mock_diag,
        ):
            handle_run_action(state)
        mock_diag.assert_called_once_with("filemoon.sx", ok=False, exc="DownloadFailedError")

    def test_failure_on_last_remaining_player_skips_episode(self):
        state = download_state(failed_ids={PLAYER.online_id}, chosen_player=PLAYER2)
        with (
            patch(_DOWNLOAD_RUN, side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = handle_run_action(state)
        assert result is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1
        assert state.completed_eps == set()
        assert state.engine.history.watch_calls == []
        assert mock_warn.call_args.args[0] == _PROG["no_player_worked"].format(number=EP1.number)

    def test_failure_with_single_player_skips_episode(self):
        state = download_state(players=(PLAYER,))
        with (
            patch(_DOWNLOAD_RUN, side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            result = handle_run_action(state)
        assert result is Screen.EPISODE_DISPATCH
        assert state.ep_idx == 1

    def test_args_download_failure_also_returns_to_player_pick(self):
        state = download_state(episode_action=None, options=CliOptions(download=True))
        with (
            patch(_DOWNLOAD_RUN, side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            result = handle_run_action(state)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0


@pytest.mark.unit
class TestDownloadRetryOnAnotherPlayer:
    """Episode → player A → download failure → PLAYER_PICK → player B → download success → next episode."""

    def _drive(self, state, start: Screen) -> list[Screen]:
        screens = [start]
        screen = start
        while not (screen is Screen.EPISODE_DISPATCH and state.ep_idx == 1):
            screen = HANDLERS[screen](state)
            screens.append(screen)
            assert len(screens) < 20, screens
        return screens

    def test_failed_download_retries_on_picked_player_without_asking_action(self):
        state = download_state(resolved=None)
        streams = {PLAYER.online_id: STREAM, PLAYER2.online_id: STREAM_B}
        state.session.resolve_script = lambda call: streams[call.chosen.online_id]
        fake = FakeDownloads(fail_urls={STREAM.default.url})

        with (
            patch(_DOWNLOAD_RUN, side_effect=fake),
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", PLAYER2)) as mock_pick,
            patch("alt_ani_cli.ui.menus.select_action") as mock_action,
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
        assert [call["variant"].url for call in fake.calls] == [STREAM.default.url, STREAM_B.default.url]
        assert mock_pick.call_args.kwargs["failed"] == {PLAYER.online_id}
        mock_action.assert_not_called()
        assert state.engine.history.watch_calls == []
        assert state.engine.history.download_calls == [(SERIES_REF, EP1.number)]
        assert state.episode_action == "download"


@pytest.mark.unit
class TestRunActionDownloadHistory:
    def test_success_records_download_and_updates_state(self):
        state = download_state(downloaded_eps={5.0})
        with patch(_DOWNLOAD_RUN):
            handle_run_action(state)
        assert state.engine.history.download_calls == [(SERIES_REF, EP1.number)]
        assert state.engine.history.watch_calls == []
        assert state.downloaded_eps == {5.0, EP1.number}
        assert state.completed_eps == set()

    def test_failure_does_not_record_download(self):
        state = download_state()
        with (
            patch(_DOWNLOAD_RUN, side_effect=DownloadFailedError("x")),
            patch("alt_ani_cli.ui.progress.error"),
        ):
            handle_run_action(state)
        assert state.engine.history.download_calls == []
        assert state.downloaded_eps == set()

    def test_success_is_persisted_without_touching_watch_history(self):
        state = download_state()
        with patch(_DOWNLOAD_RUN):
            handle_run_action(state)
        history = state.engine.history
        assert [(e.series, e.episodes) for e in history.download_entries()] == [(SERIES_REF, frozenset({EP1.number}))]
        assert history.watch_entries() == []


@pytest.mark.unit
class TestRunActionExistingFile:
    def test_missing_file_downloads_without_prompt(self):
        state = batch_state([EP4, EP5])
        fake = FakeDownloads()
        result, prompt = run_download(state, fake)
        prompt.assert_not_called()
        assert fake.outcomes == [("downloaded", 4.0)]
        assert state.engine.history.download_calls == [(SERIES_REF, 4.0)]
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)

    def test_skip_keeps_file_and_records_download(self):
        state = batch_state([EP4, EP5])
        fake = FakeDownloads(existing={4.0})
        result, _ = run_download(state, fake, "skip")
        assert fake.outcomes == [("kept", 4.0)]
        assert state.engine.history.download_calls == [(SERIES_REF, 4.0)]
        assert state.downloaded_eps == {4.0}
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)
        assert state.overwrite_existing_batch is False

    def test_skip_reports_the_kept_path(self):
        state = batch_state([EP4])
        with (
            patch(_DOWNLOAD_RUN, side_effect=FakeDownloads(existing={4.0})),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value="skip"),
            patch("alt_ani_cli.ui.progress.info") as mock_info,
        ):
            handle_run_action(state)
        kept = FakeDownloads.path(state.paths.downloads_dir, EP4)
        mock_info.assert_called_once_with(CONTENT["download"]["kept_existing"].format(path=kept))

    def test_skip_does_not_report_download_result(self):
        state = batch_state([EP4])
        with patch("alt_ani_core.diagnostics.download_result") as mock_diag:
            run_download(state, FakeDownloads(existing={4.0}), "skip")
        mock_diag.assert_not_called()

    def test_overwrite_replaces_and_records_without_batch_policy(self):
        state = batch_state([EP4, EP5])
        fake = FakeDownloads(existing={4.0})
        result, _ = run_download(state, fake, "overwrite")
        assert fake.outcomes == [("replaced", 4.0)]
        assert state.engine.history.download_calls == [(SERIES_REF, 4.0)]
        assert state.downloaded_eps == {4.0}
        assert (result, state.ep_idx) == (Screen.EPISODE_DISPATCH, 1)
        assert state.overwrite_existing_batch is False

    def test_cancel_returns_to_episode_pick_without_history(self):
        state = batch_state([EP4, EP5, EP6])
        fake = FakeDownloads(existing={4.0})
        result, _ = run_download(state, fake, "cancel")
        assert result is Screen.EPISODES_PICK
        assert fake.outcomes == [("kept", 4.0)]
        assert state.engine.history.download_calls == []
        assert state.downloaded_eps == set()
        assert state.ep_idx == 0
        assert state.overwrite_existing_batch is False
        assert state.resolved is None

    def test_cancel_warns_that_the_batch_was_cancelled(self):
        state = batch_state([EP4, EP5])
        with (
            patch(_DOWNLOAD_RUN, side_effect=FakeDownloads(existing={4.0})),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value="cancel"),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            handle_run_action(state)
        mock_warn.assert_called_once_with(CONTENT["download"]["batch_cancelled"])

    def test_overwrite_remaining_sets_batch_policy(self):
        state = batch_state([EP4, EP5])
        fake = FakeDownloads(existing={4.0})
        run_download(state, fake, "overwrite_remaining")
        assert fake.outcomes == [("replaced", 4.0)]
        assert state.overwrite_existing_batch is True

    def test_policy_skips_prompt_for_next_existing_episode(self):
        state = batch_state([EP4, EP5], ep_idx=1, overwrite_existing_batch=True)
        fake = FakeDownloads(existing={5.0})
        _, prompt = run_download(state, fake)
        prompt.assert_not_called()
        assert fake.outcomes == [("replaced", 5.0)]
        assert state.engine.history.download_calls == [(SERIES_REF, 5.0)]

    def test_policy_downloads_missing_episode_normally(self):
        state = batch_state([EP4, EP5], ep_idx=1, overwrite_existing_batch=True)
        fake = FakeDownloads()
        _, prompt = run_download(state, fake)
        prompt.assert_not_called()
        assert fake.outcomes == [("downloaded", 5.0)]

    def test_failed_overwrite_returns_to_player_pick_and_keeps_policy(self):
        state = batch_state([EP4, EP5], overwrite_existing_batch=True)
        fake = FakeDownloads(existing={4.0}, fail_urls={STREAM.default.url})
        result, _ = run_download(state, fake)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0
        assert state.engine.history.download_calls == []
        assert state.downloaded_eps == set()
        assert state.overwrite_existing_batch is True

    @pytest.mark.parametrize(
        ("targets", "ep_idx", "has_remaining"),
        [([EP4], 0, False), ([EP4, EP5], 0, True), ([EP4, EP5], 1, False)],
    )
    def test_batch_option_offered_only_with_remaining_episodes(self, targets, ep_idx, has_remaining):
        state = batch_state(targets, ep_idx=ep_idx)
        ep = targets[ep_idx]
        _, prompt = run_download(state, FakeDownloads(existing={ep.number}), "skip")
        assert prompt.call_args.args == (str(FakeDownloads.path(state.paths.downloads_dir, ep)),)
        assert prompt.call_args.kwargs == {"has_remaining": has_remaining}

    def test_already_downloaded_episode_still_prompts(self):
        state = batch_state([EP4], downloaded_eps={4.0})
        _, prompt = run_download(state, FakeDownloads(existing={4.0}), "skip")
        prompt.assert_called_once()


@pytest.mark.unit
class TestOverwriteBatchScenario:
    def test_remaining_policy_survives_player_retry(self):
        """ep4 existing → overwrite remaining; ep5 missing; ep6 fails on player A, retried on B; ep7 auto."""
        state = batch_state([EP4, EP5, EP6, EP7])
        fake = FakeDownloads(existing={4.0, 6.0, 7.0})
        prompts: list[float] = []

        def answer(path, *, has_remaining):
            prompts.append(state.current_ep.number)
            return "overwrite_remaining"

        screens = []
        with (
            patch(_DOWNLOAD_RUN, side_effect=fake),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", side_effect=answer),
            patch("alt_ani_cli.ui.progress.error"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            for _ in range(2):
                state.resolved = resolved_for(PLAYER, STREAM)
                screens.append(handle_run_action(state))
            fake.fail_urls = {STREAM.default.url}
            state.resolved = resolved_for(PLAYER, STREAM)
            screens.append(handle_run_action(state))
            state.chosen_player = PLAYER2
            state.resolved = resolved_for(PLAYER2, STREAM_B)
            screens.append(handle_run_action(state))
            fake.fail_urls = set()
            state.resolved = resolved_for(PLAYER2, STREAM)
            screens.append(handle_run_action(state))

        assert prompts == [4.0]
        assert fake.outcomes == [
            ("replaced", 4.0),
            ("downloaded", 5.0),
            ("failed", 6.0),
            ("replaced", 6.0),
            ("replaced", 7.0),
        ]
        assert screens == [Screen.EPISODE_DISPATCH, Screen.EPISODE_DISPATCH, Screen.PLAYER_PICK] + [Screen.EPISODE_DISPATCH] * 2
        assert [episode for _, episode in state.engine.history.download_calls] == [4.0, 5.0, 6.0, 7.0]

    def test_skip_in_batch_continues_with_next_episode(self):
        state = batch_state([EP4, EP5, EP6])
        fake = FakeDownloads(existing={4.0})
        with (
            patch(_DOWNLOAD_RUN, side_effect=fake),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value="skip"),
            patch("alt_ani_cli.ui.progress.info"),
        ):
            handle_run_action(state)
            state.resolved = resolved_for(PLAYER, STREAM)
            handle_run_action(state)
        assert fake.outcomes == [("kept", 4.0), ("downloaded", 5.0)]
        assert [episode for _, episode in state.engine.history.download_calls] == [4.0, 5.0]
        assert state.ep_idx == 2


@pytest.mark.unit
class TestOverwriteBatchPolicyReset:
    def test_new_episode_selection_resets_policy(self):
        state = make_state(ref=SERIES_REF, episodes=[EP4, EP5], overwrite_existing_batch=True)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP5]):
            HANDLERS[Screen.EPISODES_PICK](state)
        assert state.overwrite_existing_batch is False

    def test_cli_episode_range_batch_resets_policy(self):
        state = make_state(
            ref=SERIES_REF, episodes=[EP4, EP5], overwrite_existing_batch=True, options=CliOptions(episode="4-5")
        )
        HANDLERS[Screen.EPISODES_PICK](state)
        assert state.overwrite_existing_batch is False

    def test_new_series_resets_policy(self):
        state = make_state(ref=SERIES_REF, overwrite_existing_batch=True)
        state.engine.episodes.return_value = EpisodeListing(SERIES_REF, [EP1])
        HANDLERS[Screen.FETCH_EPISODES](state)
        assert state.overwrite_existing_batch is False

    def test_player_pick_after_failure_keeps_policy(self):
        state = batch_state([EP4, EP5], overwrite_existing_batch=True, failed_ids={PLAYER.online_id})
        with (
            patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", PLAYER2)),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            HANDLERS[Screen.PLAYER_PICK](state)
        assert state.overwrite_existing_batch is True


@pytest.mark.unit
class TestSkipBackfillsDownloadHistory:
    def test_skip_on_file_unknown_to_history_marks_it_downloaded(self):
        state = batch_state([EP4])
        history = state.engine.history
        assert history.download_entries() == []
        run_download(state, FakeDownloads(existing={4.0}), "skip")
        assert [(e.series, e.episodes) for e in history.download_entries()] == [(SERIES_REF, frozenset({4.0}))]

    def test_skipped_episode_is_marked_on_next_download_resume(self):
        state = batch_state([EP4])
        run_download(state, FakeDownloads(existing={4.0}), "skip")
        resumed = make_state(engine=state.engine)
        with patch("alt_ani_cli.ui.menus.select_series_from_download_history", side_effect=lambda e: e[0]):
            HANDLERS[Screen.DOWNLOAD_RESUME_PICK](resumed)
        assert resumed.downloaded_eps == {4.0}


@pytest.mark.unit
class TestRunActionDownloadTargetError:
    def _run(self, state, exc=None):
        exc = exc or DownloadTargetError("C:/dl/Fate - ep4.mp4 locked")
        with (
            patch(_DOWNLOAD_RUN, side_effect=exc),
            patch("alt_ani_core.diagnostics.download_result") as mock_diag,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
        ):
            result = handle_run_action(state)
        return result, mock_diag, mock_error

    def test_local_file_error_does_not_fail_the_player(self):
        state = batch_state([EP4, EP5], overwrite_existing_batch=True)
        result, diag, error = self._run(state)
        assert result is Screen.PLAYER_PICK
        assert state.failed_ids == set()
        assert state.expired_ids == set()
        assert state.ep_idx == 0
        assert state.overwrite_existing_batch is True
        assert state.resolved is None
        assert state.engine.history.download_calls == []
        diag.assert_not_called()
        assert error.call_args.args[0] == "C:/dl/Fate - ep4.mp4 locked"

    def test_coded_error_is_shown_in_polish(self):
        state = batch_state([EP4])
        exc = DownloadTargetError("could not replace the file").coded("download.replace_failed", path="C:/dl/Fate - ep4.mp4")
        _, _, error = self._run(state, exc)
        error.assert_called_once_with(EXCEPTIONS_PL["download"]["replace_failed"].format(path="C:/dl/Fate - ep4.mp4"))

    def test_single_player_episode_is_not_skipped(self):
        state = batch_state([EP4, EP5], players=(PLAYER,))
        result, *_ = self._run(state)
        assert result is Screen.PLAYER_PICK
        assert state.ep_idx == 0
        assert state.failed_ids == set()

    def test_player_pick_offers_same_player_again(self):
        state = batch_state([EP4])
        self._run(state)
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("pick", PLAYER)) as mock_pick:
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.RESOLVE_STREAM
        assert mock_pick.call_args.kwargs["failed"] == set()
        assert state.chosen_player is PLAYER
        assert HANDLERS[Screen.RESOLVE_STREAM](state) is Screen.ACTION_PICK
        assert [call.force_fresh for call in state.session.resolve_calls] == [False]
