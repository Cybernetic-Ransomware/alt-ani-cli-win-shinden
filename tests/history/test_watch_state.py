import pytest

from alt_ani_core.errors import UnknownRecordError
from alt_ani_core.state import OpenedVia, WatchedSource

from .state_support import download, enqueue, key, open_state, ref, row


@pytest.fixture
def state(tmp_path):
    return open_state(tmp_path / "history.db")


@pytest.fixture
def opened(state):
    state.watch.record_opened(ref(), row(3), OpenedVia.PLAYER)
    return state


@pytest.mark.unit
class TestIndependentFields:
    def test_opening_records_neither_position_nor_watched(self, opened):
        watch = opened.watch.get(key(3))
        assert watch.opened_via is OpenedVia.PLAYER and watch.last_opened_at is not None
        assert (watch.position_ms, watch.duration_ms, watch.position_at) == (None, None, None)
        assert watch.watched is False

    def test_unknown_episode_has_no_state(self, state):
        assert state.watch.get(key(1)) is None

    def test_position_does_not_mark_watched(self, opened):
        session = opened.watch.begin_playback(key(3))
        assert opened.watch.record_position(session, 1, 1_400_000, 1_420_000)
        watch = opened.watch.get(key(3))
        assert (watch.position_ms, watch.duration_ms, watch.watched) == (1_400_000, 1_420_000, False)

    def test_reaching_the_end_marks_watched(self, opened):
        session = opened.watch.begin_playback(key(3))
        assert opened.watch.record_playback_ended(session, 1, 1_420_000, 1_420_000)
        watch = opened.watch.get(key(3))
        assert watch.watched and watch.watched_source is WatchedSource.PLAYBACK_ENDED

    def test_manual_unwatch_keeps_position_and_opening(self, opened):
        session = opened.watch.begin_playback(key(3))
        opened.watch.record_playback_ended(session, 1, 1_000, 1_000)
        before = opened.watch.get(key(3))
        after = opened.watch.set_watched(key(3), False)
        assert after.watched is False and after.watched_source is WatchedSource.MANUAL
        assert (after.position_ms, after.last_opened_at) == (before.position_ms, before.last_opened_at)

    def test_manual_watch_needs_no_playback(self, opened):
        watch = opened.watch.set_watched(key(3), True)
        assert watch.watched and watch.position_ms is None

    def test_manual_change_of_an_unknown_episode_is_refused(self, state):
        with pytest.raises(UnknownRecordError) as info:
            state.watch.set_watched(key(9), True)
        assert info.value.code == "state.unknown_episode"


@pytest.mark.unit
class TestBrowserExit:
    def test_browser_updates_opening_and_keeps_progress_and_status(self, opened):
        session = opened.watch.begin_playback(key(3))
        opened.watch.record_position(session, 1, 600_000, 1_400_000)
        opened.watch.set_watched(key(3), True)
        before = opened.watch.get(key(3))
        after = opened.watch.record_opened(ref(), row(3), OpenedVia.BROWSER)
        assert after.opened_via is OpenedVia.BROWSER and after.last_opened_at > before.last_opened_at
        assert (after.position_ms, after.duration_ms, after.watched) == (600_000, 1_400_000, True)

    def test_player_session_still_records_after_a_browser_opening(self, opened):
        session = opened.watch.begin_playback(key(3))
        opened.watch.record_opened(ref(), row(3), OpenedVia.BROWSER)
        assert opened.watch.record_position(session, 1, 5_000)


@pytest.mark.unit
class TestPlaybackSessions:
    def test_late_write_of_a_superseded_session_is_refused(self, opened):
        old = opened.watch.begin_playback(key(3))
        assert opened.watch.record_position(old, 1, 900_000)
        new = opened.watch.begin_playback(key(3))
        assert opened.watch.record_position(new, 1, 10_000)
        assert not opened.watch.record_position(old, 2, 950_000)
        assert not opened.watch.record_playback_ended(old, 3, 1_400_000, 1_400_000)
        watch = opened.watch.get(key(3))
        assert (watch.position_ms, watch.watched) == (10_000, False)

    def test_rewinding_within_a_session_is_kept(self, opened):
        session = opened.watch.begin_playback(key(3))
        opened.watch.record_position(session, 1, 900_000)
        assert opened.watch.record_position(session, 2, 0)
        assert opened.watch.get(key(3)).position_ms == 0

    def test_out_of_order_write_is_refused(self, opened):
        session = opened.watch.begin_playback(key(3))
        assert opened.watch.record_position(session, 5, 500_000)
        assert not opened.watch.record_position(session, 4, 400_000)
        assert not opened.watch.record_position(session, 5, 450_000)
        assert opened.watch.get(key(3)).position_ms == 500_000

    def test_new_session_keeps_the_known_position_for_resuming(self, opened):
        old = opened.watch.begin_playback(key(3))
        opened.watch.record_position(old, 1, 700_000)
        opened.watch.begin_playback(key(3))
        assert opened.watch.get(key(3)).position_ms == 700_000

    def test_sessions_of_other_episodes_are_independent(self, opened):
        opened.watch.record_opened(ref(), row(4), OpenedVia.PLAYER)
        s3 = opened.watch.begin_playback(key(3))
        opened.watch.begin_playback(key(4))
        assert opened.watch.record_position(s3, 1, 1_000)

    def test_session_of_an_unknown_episode_is_refused(self, state):
        with pytest.raises(UnknownRecordError):
            state.watch.begin_playback(key(9))

    @pytest.mark.parametrize("seq,position,duration", [(0, 0, None), (-1, 0, None), (1, -5, None), (1, 0, 0), (True, 0, None)])
    def test_invalid_values_are_rejected(self, opened, seq, position, duration):
        session = opened.watch.begin_playback(key(3))
        with pytest.raises(ValueError):
            opened.watch.record_position(session, seq, position, duration)


@pytest.mark.unit
class TestResumeAndList:
    def test_last_opened_is_the_most_recent_opening(self, state):
        for number in (1, 2, 5, 3):
            state.watch.record_opened(ref(), row(number), OpenedVia.PLAYER)
        assert state.watch.last_opened("1").key == key(3)

    def test_last_opened_ignores_episodes_never_opened(self, state):
        state.watch.record_opened(ref(), row(2), OpenedVia.PLAYER)
        enqueue(state, 7)
        state.watch.set_watched(key(7), True)
        assert state.watch.last_opened("1").key == key(2)

    def test_list_is_ordered_by_number_with_earlier_episodes_first(self, state):
        for number in (3, 1, 2.5):
            state.watch.record_opened(ref(), row(number), OpenedVia.PLAYER)
        assert [w.key.number for w in state.watch.for_series("1")] == [1.0, 2.5, 3.0]

    def test_recent_series_lists_each_series_once_with_its_latest_episode(self, state):
        state.watch.record_opened(ref("1"), row(1), OpenedVia.PLAYER)
        state.watch.record_opened(ref("2", "Bleach"), row(4), OpenedVia.PLAYER)
        state.watch.record_opened(ref("1"), row(2), OpenedVia.BROWSER)
        recent = state.watch.recent_series()
        assert [(series.id, watch.key.number) for series, watch in recent] == [("1", 2.0), ("2", 4.0)]


@pytest.mark.unit
class TestProgressFollowsTheEpisode:
    def test_position_is_kept_when_a_local_copy_appears_and_is_deleted(self, opened):
        session = opened.watch.begin_playback(key(3))
        opened.watch.record_position(session, 1, 300_000)
        _, item = download(opened, 3)
        assert opened.watch.get(key(3)).position_ms == 300_000
        opened.library.mark_deleting([item.id])
        opened.library.forget(item.id)
        assert opened.watch.get(key(3)).position_ms == 300_000
