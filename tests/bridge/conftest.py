import json
import sys
from pathlib import Path

import pytest
from alt_ani_core.testing import FakeTransport, InMemoryHistory

BRIDGE_SRC = Path(__file__).parents[2] / "apps" / "android" / "app" / "src" / "main" / "python"
if str(BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(BRIDGE_SRC))

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

    def __init__(self, bridge, transport: FakeTransport, history: InMemoryHistory, tmp_path: Path) -> None:
        self.bridge = bridge
        self.transport = transport
        self.history = history
        self.tmp_path = tmp_path
        self.events: list[dict] = []
        self.engine_id = ""
        self.episode = EPISODE
        self.fixtures = FIXTURES
        self.page_token = TEST_TOKEN

    def create(self, listener=None, **overrides) -> dict:
        payload = {
            "cache_dir": str(self.tmp_path / "cache"),
            "history_path": str(self.tmp_path / "history.db"),
            "antibot_delay_sec": 0,
            "access": {"use_flaresolverr": False},
            **overrides,
        }
        on_event = listener if listener is not None else (lambda message: self.events.append(json.loads(message)))
        reply = json.loads(self.bridge.create_engine(json.dumps(payload), on_event))
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
