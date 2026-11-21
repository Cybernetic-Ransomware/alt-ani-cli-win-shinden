"""The bridge path the Android device tests drive, on the desktop against the controlled server."""

import json
import time
from pathlib import Path

import android_storage
import controlled_host

from tools.android.controlled_server import Scenario, Server


class Flow:
    """``storage`` is ``fs`` (file targets), ``content`` (content URIs) or None (no file store, as before PR #51)."""

    def __init__(self, server: Server, tmp_path: Path, storage: str | None = None, api_level: int | None = None) -> None:
        self.server = server
        self.root = tmp_path / ("lib" if storage == "content" else "library")
        self.bridge = controlled_host.create_bridge(server.url)
        if storage == "fs":
            extra = {"storage": json.loads(android_storage.register(self.bridge, str(self.root), api_level=api_level))}
        elif storage == "content":
            registered = android_storage.register(self.bridge, str(self.root), "a.b", "lib", api_level)
            extra = {"storage": json.loads(registered)}
        else:
            extra = {}
        created = self._reply(
            self.bridge.create_engine(
                json.dumps(
                    {
                        "cache_dir": str(tmp_path / "cache"),
                        "history_path": str(tmp_path / "history.db"),
                        "antibot_delay_sec": 0,
                        "access": {"use_flaresolverr": False},
                        "network": "wifi",
                        **extra,
                    }
                ),
                None,
            )
        )
        self.engine_id = created["data"]["engine_id"]

    @staticmethod
    def _reply(text: str) -> dict:
        return json.loads(text)

    def call(self, method: str, **payload) -> dict:
        return self._reply(self.bridge.call(method, json.dumps({"engine_id": self.engine_id, **payload})))

    def data(self, method: str, **payload) -> dict:
        reply = self.call(method, **payload)
        assert reply["ok"], reply
        return reply["data"]

    def error(self, method: str, **payload) -> dict:
        reply = self.call(method, **payload)
        assert not reply["ok"], reply
        return reply["error"]

    def listing(self) -> tuple[dict, dict]:
        hit = self.data("search", query="frieren")["hits"][0]
        listing = self.data("episodes", series={k: hit[k] for k in ("id", "slug", "title", "url")})
        return listing["series"], listing["episodes"][0]

    def prepare(self, scenario: Scenario, **criteria) -> tuple[dict, dict, dict, dict]:
        self.server.state.reset(scenario)
        series, episode = self.listing()
        session = self.data("playback_begin", series=series, episode=episode)["playback_session"]
        extra = {"criteria": criteria} if criteria else {}
        prepared = self.data("playback_prepare", playback_session=session, series=series, episode=episode, **extra)
        return series, episode, session, prepared

    def enqueue(self) -> tuple[dict, dict, str]:
        series, episode = self.listing()
        selection = {"mode": "episodes", "numbers": [episode["number"]]}
        plan = self.data("queue_preview", series=series, episodes=[episode], selection=selection)["plan"]
        (job,) = self.data("queue_enqueue", plan=plan)["added"]
        return series, episode, job["job_id"]

    def job(self, job_id: str) -> dict:
        return self.data("queue_job", job_id=job_id)["job"]

    def wait_job(self, job_id: str, *states: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while (job := self.job(job_id))["state"] not in states:
            assert time.monotonic() < deadline, job
            time.sleep(0.05)
        return job

    def local_target(self, series: dict, episode: dict) -> dict:
        session = self.data("playback_begin", series=series, episode=episode)["playback_session"]
        return self.data("playback_prepare", playback_session=session, series=series, episode=episode)

    def shutdown(self) -> None:
        assert json.loads(self.bridge.shutdown(10))["data"]["ended"]


def wait_until(predicate, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while not (value := predicate()):
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.05)
    return value
