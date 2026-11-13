"""Stream URLs, headers, embed tokens and the page token appear only in the responses that hand them to the player."""

import json
import logging
from unittest.mock import patch

import pytest
from history.state_support import dump_database

from alt_ani_core.errors import NoStreamError
from alt_ani_core.services import episode_session
from alt_ani_core.testing import make_stream

from .conftest import EPISODE_URL, SERIES, TEST_TOKEN, ep, make_v2

HEADER_SECRET = "HEADERSECRETVALUE"
SENTINELS = ("SECRETEMBEDTOKEN", "STREAMSIG", HEADER_SECRET, TEST_TOKEN)
STREAM = make_stream(
    "https://cdn.example/master.m3u8?sig=STREAMSIG",
    headers={"Authorization": HEADER_SECRET},
    qualities={"1080p": "https://cdn.example/1080.m3u8?sig=STREAMSIG"},
    ext="m3u8",
)


def leaked(text: str) -> list[str]:
    return [s for s in SENTINELS if s in text]


@pytest.fixture
def diag_dir(tmp_path):
    yield tmp_path / "diag"
    logger = logging.getLogger("alt_ani_core.diagnostics")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()


def test_secrets_stay_in_player_responses(tmp_path, diag_dir):
    client = make_v2(tmp_path)
    pushed: list[str] = []
    client.create(
        listener=pushed.append, storage={"kind": "fs", "root": str(tmp_path / "library")}, diagnostics_dir=str(diag_dir)
    )
    cursor = client.head()
    episode = ep(1, EPISODE_URL)
    with patch.object(episode_session.extract, "resolve", return_value=STREAM):
        session = client.data("playback_begin", series=SERIES, episode=episode)["playback_session"]
        prepared = client.data("playback_prepare", playback_session=session, series=SERIES, episode=episode)
        refreshed = client.data("stream_refresh", session_id=prepared["session_id"], playback_session=session)
        opened = client.data(
            "watch_open_in_browser",
            series=SERIES,
            episode=episode,
            session_id=prepared["session_id"],
            online_id=prepared["source"]["online_id"],
        )
    # the player responses carry what the player needs
    assert "STREAMSIG" in json.dumps(prepared) and HEADER_SECRET in json.dumps(refreshed)
    assert "SECRETEMBEDTOKEN" in opened["url"]
    with patch.object(episode_session.extract, "resolve", side_effect=NoStreamError("https://cdn/?sig=STREAMSIG")):
        error = client.error("stream_refresh", session_id=prepared["session_id"], playback_session=session)
    (job,) = client.enqueue((1,), episodes=[episode], selection={"mode": "episodes", "numbers": [1]})
    others = [
        error,
        client.data("queue_snapshot"),
        client.data("queue_job_sources", job_id=job["job_id"])["partial"],
        client.data("library_list"),
        client.data("watch_get", series_id="7"),
        client.data("engine_status"),
        client.data("events_status"),
        client.data("watch_series", series_id="7"),
    ]
    events, _ = client.drain(cursor)
    (runtime,) = client.bridge._runtimes.values()
    entry = next(iter(client.bridge._engines.values()))
    state = [runtime.snapshot_for_tests(), [(s.selection, s.playback_session_id) for s in entry.sessions.values()]]
    db = dump_database(tmp_path / "history.db")
    client.bridge.shutdown(5)
    logs = "".join(p.read_text(encoding="utf-8") for p in diag_dir.rglob("*") if p.is_file())
    assert leaked(json.dumps(others)) == []
    assert leaked(json.dumps(events)) == [] and leaked("".join(pushed)) == []
    assert leaked(repr(state)) == []
    assert leaked(db) == []
    assert logs and leaked(logs) == []
