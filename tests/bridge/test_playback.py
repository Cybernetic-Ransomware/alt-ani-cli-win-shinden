"""Playback through the contract: local copy first, explicit criteria, consent per viewing, refresh keeps the choice."""

from pathlib import Path
from unittest.mock import patch

import pytest

from alt_ani_core.services import episode_session
from alt_ani_core.testing import make_stream

from .conftest import EPISODE_URL, SERIES, ep, make_v2

STREAM = make_stream(
    "https://cdn.example/master.m3u8?sig=STREAMSIG",
    headers={"Referer": "https://host.example/"},
    qualities={"1080p": "https://cdn.example/1080.m3u8?sig=STREAMSIG", "480p": "https://cdn.example/480.m3u8"},
    ext="m3u8",
)
ONLINE_EP = ep(1, EPISODE_URL)


@pytest.fixture
def extract_ok():
    with patch.object(episode_session.extract, "resolve", return_value=STREAM) as mock:
        yield mock


def begin(client, episode=ONLINE_EP) -> dict:
    return client.data("playback_begin", series=SERIES, episode=episode)["playback_session"]


def prepare(client, session, episode=ONLINE_EP, **kw) -> dict:
    return client.call("playback_prepare", playback_session=session, series=SERIES, episode=episode, **kw)


class TestLocalFirst:
    def test_matching_local_copy_needs_no_network(self, v2):
        (item,) = v2.download(1)
        v2.data("network_set", kind="offline")
        session = begin(v2, ep(1))
        data = prepare(v2, session, ep(1))["data"]
        assert data["mode"] == "local" and data["item_id"] == item["item_id"]
        assert data["target"]["kind"] == "file" and Path(data["target"]["value"]).is_file()
        assert v2.data("engine_status")["engine_started"] is False

    def test_non_matching_copy_is_only_offered(self, v2, extract_ok):
        (item,) = v2.download(1)
        session = begin(v2)
        data = prepare(v2, session, criteria={"subs": "en"})["data"]
        assert data["mode"] == "online"
        assert [c["item_id"] for c in data["other_local_copies"]] == [item["item_id"]]
        assert data["source"]["lang_subs"] == "en" and data["matches_criteria"] is True

    def test_watched_episode_keeps_its_real_progress(self, v2):
        (item,) = v2.download(1)
        session = begin(v2, ep(1))
        v2.data("playback_ended", playback_session=session, seq=1, position_ms=60000, duration_ms=60000)
        again = begin(v2, ep(1))
        resume = prepare(v2, again, ep(1))["data"]["resume"]
        assert resume == {"position_ms": 60000, "duration_ms": 60000, "watched": True, "watched_source": "playback_ended"}


class TestCriteria:
    @pytest.mark.parametrize(("criteria", "reason"), [({"audio": "pl"}, "audio"), ({"subs": "de"}, "subs")])
    def test_no_match_without_widening(self, v2, criteria, reason):
        error = prepare(v2, begin(v2), criteria=criteria)["error"]
        assert (error["code"], error["kind"], error["params"]) == ("criteria.no_match", "no_match", {"reason": reason})

    def test_quality_limit_and_no_default_fallback(self, v2, extract_ok):
        data = prepare(v2, begin(v2), criteria={"quality": "max:480"})["data"]
        assert data["variant"]["label"] == "480p" and data["variant"]["identity"]["height"] == 480
        error = prepare(v2, begin(v2), criteria={"quality": "max:240"})["error"]
        assert error["params"] == {"reason": "quality"}

    def test_preferences_are_the_default_criteria(self, v2, extract_ok):
        v2.data("preferences_update", changes={"quality": "max:480"}, expected_revision=0)
        assert prepare(v2, begin(v2))["data"]["criteria"]["quality"] == "max:480"


class TestRefresh:
    def test_keeps_variant_and_criteria(self, v2, extract_ok):
        session = begin(v2)
        first = prepare(v2, session, criteria={"quality": "max:480"})["data"]
        refreshed = v2.data("stream_refresh", session_id=first["session_id"], playback_session=session)
        assert refreshed["variant"]["label"] == "480p" and refreshed["variant_changed"] is False
        assert refreshed["criteria"]["quality"] == "max:480"

    def test_manual_variant_switch(self, v2, extract_ok):
        session = begin(v2)
        first = prepare(v2, session)["data"]
        wanted = next(v for v in first["variants"] if v["label"] == "480p")
        switched = v2.data("stream_refresh", session_id=first["session_id"], playback_session=session, variant=wanted)
        assert switched["variant_changed"] is True and switched["variant"]["label"] == "480p"

    def test_without_a_selection(self, v2):
        session_id = v2.data("open_episode", episode=ONLINE_EP)["session_id"]
        error = v2.error("stream_refresh", session_id=session_id, playback_session=begin(v2))
        assert error["params"]["problem"] == "no_playback_selection"

    def test_fresh_resolves_are_bounded(self, v2, extract_ok):
        session = begin(v2)
        first = prepare(v2, session)["data"]
        for _ in range(episode_session.MAX_FRESH_RESOLVES):
            v2.data("stream_refresh", session_id=first["session_id"], playback_session=session)
        error = v2.error("stream_refresh", session_id=first["session_id"], playback_session=session)
        assert (error["code"], error["kind"]) == ("episode.fresh_resolve_limit", "limit")


class TestCellularConsent:
    def test_consent_covers_the_current_viewing_only(self, v2, extract_ok):
        v2.data("network_set", kind="cellular")
        session = begin(v2)
        error = prepare(v2, session)["error"]
        assert (error["code"], error["kind"]) == ("network.cellular_consent_required", "waiting")
        v2.data("playback_allow_cellular", playback_session=session)
        first = prepare(v2, session)["data"]
        # survives a source change and a refresh
        other = prepare(v2, session, online_id=first["source"]["online_id"])["data"]
        v2.data("stream_refresh", session_id=other["session_id"], playback_session=session)
        # another episode's viewing ends it
        begin(v2, ep(2))
        error = v2.error("stream_refresh", session_id=other["session_id"], playback_session=session)
        assert error["code"] == "network.cellular_consent_required"

    def test_ended_and_close_end_the_consent(self, v2, extract_ok):
        v2.data("network_set", kind="cellular")
        session = begin(v2)
        v2.data("playback_allow_cellular", playback_session=session)
        v2.data("playback_close", playback_session=session)
        assert v2.error("playback_allow_cellular", playback_session=session)["code"] == "playback.not_open"
        assert prepare(v2, session)["error"]["code"] == "network.cellular_consent_required"

    def test_consent_does_not_reach_download_jobs(self, v2):
        v2.data("network_set", kind="cellular")
        session = begin(v2)
        v2.data("playback_allow_cellular", playback_session=session)
        (job,) = v2.enqueue((3,))
        assert job["cellular_allowed"] is False

    def test_switch_to_mobile_data_asks_open_viewings(self, v2):
        cursor = v2.head()
        session = begin(v2)
        v2.data("network_set", kind="cellular")
        event, _ = v2.wait_event(cursor, lambda e: e["type"] == "playback.consent_required")
        assert event["data"]["playback_session"]["id"] == session["id"]

    def test_offline(self, v2):
        error = (v2.data("network_set", kind="offline"), prepare(v2, begin(v2)))[1]["error"]
        assert (error["code"], error["kind"]) == ("network.offline", "unavailable")


class TestNetworkAndSessions:
    def test_solver_change_closes_sessions(self, tmp_path):
        client = make_v2(tmp_path)
        solver = {"use_flaresolverr": True, "flaresolverr_url": "http://127.0.0.1:9/solver"}
        client.create(push=False, network="cellular", access=solver)
        session_id = client.data("open_episode", episode=ONLINE_EP)["session_id"]
        cursor = client.head()
        assert client.data("network_set", kind="wifi")["sessions_closed"] == 1
        event, _ = client.wait_event(cursor, lambda e: e["type"] == "session.closed")
        assert event["data"] == {"session_id": session_id, "reason": "network_changed"}
        client.bridge.shutdown(5)


class TestBrowserAndNext:
    def test_browser_gets_the_source_when_known(self, v2, extract_ok):
        session = begin(v2)
        first = prepare(v2, session)["data"]
        opened = v2.data(
            "watch_open_in_browser",
            series=SERIES,
            episode=ONLINE_EP,
            session_id=first["session_id"],
            online_id=first["source"]["online_id"],
        )
        assert opened["target"] == "source" and opened["url"].startswith("https://host.example/")

    def test_session_of_another_series_is_refused(self, v2, extract_ok):
        first = prepare(v2, begin(v2))["data"]
        other_series = {**SERIES, "id": "8", "url": "https://shinden.pl/series/8-other"}
        other = v2.data("playback_begin", series=other_series, episode=ONLINE_EP)["playback_session"]
        error = v2.error(
            "playback_prepare",
            playback_session=other,
            series=other_series,
            episode=ONLINE_EP,
            session_id=first["session_id"],
        )
        assert (error["code"], error["params"]) == ("bad_request", {"field": "session_id", "problem": "other_episode"})
        opened = v2.error(
            "watch_open_in_browser",
            series=other_series,
            episode=ONLINE_EP,
            session_id=first["session_id"],
            online_id=first["source"]["online_id"],
        )
        assert opened["params"]["problem"] == "other_episode"
        assert v2.data("watch_get", series_id="8", numbers=[1])["episodes"][0]["opened_via"] == "player"

    def test_session_of_another_episode_page_is_refused(self, v2, extract_ok):
        first = prepare(v2, begin(v2))["data"]
        moved = {**ONLINE_EP, "url": EPISODE_URL + "-other"}
        error = v2.error(
            "playback_prepare", playback_session=begin(v2, moved), series=SERIES, episode=moved, session_id=first["session_id"]
        )
        assert error["params"]["problem"] == "other_episode"

    def test_next_follows_the_known_order(self, v2):
        v2.download(3)
        data = v2.data("episode_next", series_id="7", current=1, episodes=[1, 2, 3])
        assert data["basis"] == "listing"
        assert data["next"] == {"number": 2, "local_copy": None}
        assert data["later_local_copy"]["number"] == 3

    def test_without_listing_only_a_separate_proposal(self, v2):
        v2.download(3)
        data = v2.data("episode_next", series_id="7", current=1)
        assert data["basis"] == "none" and data["next"] is None
        assert data["later_local_copy"]["number"] == 3

    def test_local_next(self, v2):
        (item,) = v2.download(2)
        data = v2.data("episode_next", series_id="7", current=1, episodes=[1, 2])
        assert data["next"]["local_copy"]["item_id"] == item["item_id"]
        assert data["later_local_copy"] is None
