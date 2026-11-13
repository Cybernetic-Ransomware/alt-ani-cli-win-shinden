import json
import threading
import time
from pathlib import Path

import pytest
from offline.support import FakeSources, GatedTransfer, fast_config, player, progressive

from alt_ani_core.testing import FakeTransport, InMemoryHistory

FIXTURES = Path(__file__).parent.parent / "fixtures" / "shinden"
EPISODE_URL = "https://shinden.pl/episode/61064-sousou-no-frieren/view/227851"
EPISODE = {"number": 1, "title": "Episode 1", "url": EPISODE_URL}
TEST_TOKEN = "VGVzdFRva2VuRm9yVW5pdFRlc3RzT25seQ+/=="
_ISLAND = '<div class="ep-players__island" data-sh-module="episode-players">'
IFRAME = '<iframe src="//host.example/e/abc?token=SECRETEMBEDTOKEN"></iframe>'


def episode_html() -> str:
    html = (FIXTURES / "episode_players.html").read_text(encoding="utf-8")
    return html.replace(_ISLAND, _ISLAND[:-1] + f' data-player-auth="{TEST_TOKEN}">', 1)


class Client:
    """Speaks to the bridge the way the Android host does: JSON strings in, JSON envelopes out."""

    def __init__(self, bridge, transport: FakeTransport, history: InMemoryHistory | None, tmp_path: Path) -> None:
        self.bridge = bridge
        self.transport = transport
        self.history = history
        self.tmp_path = tmp_path
        self.events: list[dict] = []
        self.engine_id = ""
        self.episode = EPISODE
        self.fixtures = FIXTURES
        self.page_token = TEST_TOKEN

    def create(self, listener=None, *, push: bool = True, **overrides) -> dict:
        payload = {
            "cache_dir": str(self.tmp_path / "cache"),
            "history_path": str(self.tmp_path / "history.db"),
            "antibot_delay_sec": 0,
            "access": {"use_flaresolverr": False},
            **overrides,
        }
        on_event = listener if listener is not None else (lambda message: self.events.append(json.loads(message)))
        reply = json.loads(self.bridge.create_engine(json.dumps(payload), on_event if push else None))
        if reply["ok"]:
            self.engine_id = reply["data"]["engine_id"]
        return reply

    def call(self, method: str, **payload) -> dict:
        payload.setdefault("engine_id", self.engine_id)
        return json.loads(self.bridge.call(method, json.dumps(payload)))

    def data(self, method: str, **payload) -> dict:
        reply = self.call(method, **payload)
        assert reply["ok"], reply
        return reply["data"]

    def error(self, method: str, **payload) -> dict:
        reply = self.call(method, **payload)
        assert not reply["ok"], reply
        return reply["error"]

    def close(self) -> dict:
        return json.loads(self.bridge.close_engine(json.dumps({"engine_id": self.engine_id})))


@pytest.fixture
def client(tmp_path):
    from alt_ani_bridge import Bridge

    transport = FakeTransport()
    transport.route(EPISODE_URL, lambda request: episode_html())
    transport.route(lambda request: "/player_load" in request.url, "0")
    transport.route(lambda request: "/player_show" in request.url, IFRAME)
    history = InMemoryHistory()
    bridge = Bridge(transport_factory=lambda: transport, history_factory=lambda path: history)
    client = Client(bridge, transport, history, tmp_path)
    client.create()
    yield client
    client.close()


SERIES = {"id": "7", "slug": "fate", "title": "Fate Zero", "url": "https://shinden.pl/series/7-fate"}


def ep(number: float, url: str | None = None) -> dict:
    return {
        "number": number,
        "title": f"Episode {number:g}",
        "url": url or f"https://shinden.pl/episode/7-fate/view/{number:g}",
    }


class BridgeSources(FakeSources):
    """The executor's sources in bridge tests; records the access the bridge hands it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.base_access: list = []

    def set_base_access(self, access) -> None:
        self.base_access.append(access)


class V2Client(Client):
    def plan(self, numbers=(1,), **preview) -> dict:
        listing = preview.pop("episodes", [ep(n) for n in numbers])
        selection = preview.pop("selection", {"mode": "episodes", "numbers": list(numbers)})
        return self.data("queue_preview", series=SERIES, episodes=listing, selection=selection, **preview)

    def enqueue(self, numbers=(1,), **preview) -> list[dict]:
        return self.data("queue_enqueue", plan=self.plan(numbers, **preview)["plan"])["added"]

    def job(self, job_id: str) -> dict:
        return self.data("queue_job", job_id=job_id)["job"]

    def wait_job(self, job_id: str, *states: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.job(job_id)
            if job["state"] in states:
                return job
            time.sleep(0.01)
        raise AssertionError(f"job {job_id} never reached {states}: {self.job(job_id)}")

    def download(self, *numbers: float) -> list[dict]:
        """Runs real executor attempts to completion and returns the offline items."""
        self.transfer.release.set()
        jobs = self.enqueue(numbers)
        self.data("executor_start")
        for job in jobs:
            self.wait_job(job["job_id"], "completed")
        return [self.data("library_item", item_id=self.job(j["job_id"])["offline_item_id"])["item"] for j in jobs]

    def head(self) -> dict:
        return self.data("events_head")

    def drain(self, cursor: dict, wait_ms: int = 0, max: int = 500) -> tuple[list[dict], dict]:
        data = self.data("events_next", cursor=cursor, wait_ms=wait_ms, max=max)
        return data["events"], {"epoch": data["epoch"], "seq": data["next_seq"]}

    def wait_event(self, cursor: dict, predicate, timeout: float = 10.0) -> tuple[dict, dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events, cursor = self.drain(cursor, wait_ms=200)
            for event in events:
                if predicate(event):
                    return event, cursor
        raise AssertionError("event not seen in time")


def make_v2(tmp_path: Path, **bridge_options) -> V2Client:
    from alt_ani_bridge import Bridge

    transport = FakeTransport()
    transport.route(EPISODE_URL, lambda request: episode_html())
    transport.route(lambda request: "/player_load" in request.url, "0")
    transport.route(lambda request: "/player_show" in request.url, IFRAME)
    sources = bridge_options.pop("sources", None) or BridgeSources(
        [player("1")], {"1": lambda fresh: progressive("http://127.0.0.1:9/v.mp4")}
    )
    transfer = bridge_options.pop("transfer", None) or GatedTransfer()
    options = {"config": fast_config(), "transfer": transfer, **bridge_options.pop("executor_options", {})}
    bridge = Bridge(
        transport_factory=bridge_options.pop("transport_factory", lambda: transport),
        sources_factory=lambda runtime: sources,
        executor_options=options,
        watch_interval=bridge_options.pop("watch_interval", 0.02),
        **bridge_options,
    )
    client = V2Client(bridge, transport, None, tmp_path)
    client.sources = sources
    client.transfer = transfer
    return client


@pytest.fixture
def v2(tmp_path):
    client = make_v2(tmp_path)
    reply = client.create(push=False, storage={"kind": "fs", "root": str(tmp_path / "library")})
    assert reply["ok"], reply
    yield client
    client.transfer.io_done.set()
    client.transfer.release.set()
    shut = json.loads(client.bridge.shutdown(10))
    assert shut["ok"] and shut["data"]["ended"], shut
