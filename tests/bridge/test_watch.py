"""Watch state through the contract: three independent facts, fenced positions, revision-checked manual changes."""

import json

from .conftest import SERIES, V2Client, ep, make_v2


def begin(client, number: float = 1) -> dict:
    return client.data("playback_begin", series=SERIES, episode=ep(number))


class TestPlaybackSession:
    def test_opening_position_and_watched_are_independent(self, v2):
        begun = begin(v2)
        watch = begun["watch"]
        assert (watch["opened_via"], watch["position_ms"], watch["watched"]) == ("player", None, False)
        session = begun["playback_session"]
        v2.data("playback_position", playback_session=session, seq=1, position_ms=5000, duration_ms=60000)
        state = v2.data("watch_get", series_id="7", numbers=[1])["episodes"][0]
        assert (state["position_ms"], state["watched"]) == (5000, False)

    def test_seq_starts_at_one(self, v2):
        session = begin(v2)["playback_session"]
        error = v2.error("playback_position", playback_session=session, seq=0, position_ms=1)
        assert error["params"]["field"] == "seq"

    def test_late_and_superseded_writes_are_refused(self, v2):
        old = begin(v2)["playback_session"]
        assert v2.data("playback_position", playback_session=old, seq=2, position_ms=200)["accepted"]
        assert not v2.data("playback_position", playback_session=old, seq=1, position_ms=100)["accepted"]
        new = begin(v2)["playback_session"]
        assert not v2.data("playback_position", playback_session=old, seq=3, position_ms=300)["accepted"]
        assert v2.data("playback_position", playback_session=new, seq=1, position_ms=50)["accepted"]

    def test_ended_is_the_only_automatic_watched(self, v2):
        session = begin(v2)["playback_session"]
        v2.data("playback_position", playback_session=session, seq=1, position_ms=59000, duration_ms=60000)
        assert v2.data("watch_get", series_id="7", numbers=[1])["episodes"][0]["watched"] is False
        ended = v2.data("playback_ended", playback_session=session, seq=2, position_ms=60000, duration_ms=60000)
        assert ended["accepted"] and ended["watch"]["watched_source"] == "playback_ended"

    def test_fencing_survives_a_restart(self, v2, tmp_path):
        old = begin(v2)["playback_session"]
        begin(v2)
        json.loads(v2.bridge.shutdown(5))
        fresh = make_v2(tmp_path)
        fresh.create(push=False)
        assert not fresh.data("playback_position", playback_session=old, seq=9, position_ms=1)["accepted"]
        json.loads(fresh.bridge.shutdown(5))


class TestManualChanges:
    def test_delayed_repeat_after_the_opposite_change_conflicts(self, v2):
        begin(v2)
        state = v2.data("watch_get", series_id="7", numbers=[1])["episodes"][0]
        marked = v2.data("watch_set_watched", series_id="7", number=1, watched=True, expected_revision=state["revision"])
        v2.data("watch_set_watched", series_id="7", number=1, watched=False, expected_revision=marked["watch"]["revision"])
        error = v2.error("watch_set_watched", series_id="7", number=1, watched=True, expected_revision=state["revision"])
        assert (error["code"], error["kind"]) == ("state.conflict", "conflict")
        assert v2.data("watch_get", series_id="7", numbers=[1])["episodes"][0]["watched"] is False

    def test_revision_is_required(self, v2):
        assert v2.error("watch_set_watched", series_id="7", number=1, watched=True)["params"]["problem"] == "missing"

    def test_browser_keeps_position_and_watched(self, v2):
        session = begin(v2)["playback_session"]
        v2.data("playback_position", playback_session=session, seq=1, position_ms=7000)
        opened = v2.data("watch_open_in_browser", series=SERIES, episode=ep(1))
        assert opened["target"] == "episode_page" and opened["url"] == ep(1)["url"]
        assert (opened["watch"]["opened_via"], opened["watch"]["position_ms"]) == ("browser", 7000)

    def test_recent_and_series_views(self, v2):
        begin(v2, 1)
        begin(v2, 2)
        assert v2.data("watch_series", series_id="7")["last_opened"]["number"] == 2
        assert v2.data("watch_recent")["series"][0]["series"]["id"] == "7"
