"""The Android debug host against the controlled server: the whole bridge path the device tests drive, on the desktop."""

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import controlled_host
import pytest

from alt_ani_core.transport.api import NetworkError, SessionProfile

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.android.controlled_server import MEDIA, REQUIRED_HEADERS, Scenario, Server  # noqa: E402

pytestmark = pytest.mark.loopback


@pytest.fixture
def server():
    with Server(0) as srv:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield srv
        srv.shutdown()


class Flow:
    def __init__(self, server: Server, tmp_path: Path, storage: str | None = None) -> None:
        self.server = server
        self.bridge = controlled_host.create_bridge(server.url)
        if storage == "fs":
            extra = {"storage": json.loads(controlled_host.storage(self.bridge, str(tmp_path / "library")))}
        elif storage == "content":
            extra = {"storage": json.loads(controlled_host.storage(self.bridge, str(tmp_path / "lib"), "a.b", "lib"))}
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


@pytest.fixture
def flow(server, tmp_path):
    flow = Flow(server, tmp_path)
    yield flow
    assert json.loads(flow.bridge.shutdown(10))["data"]["ended"]


def fetch(url: str, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            reply.read()
            return reply.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_prepare_returns_a_variant_chosen_by_the_criteria_with_the_hosts_headers(flow):
    *_, prepared = flow.prepare(Scenario(kind="hls", variants=["1080p", "720p", "480p"]), quality="max:720")
    assert prepared["mode"] == "online"
    variant = prepared["variant"]
    assert (variant["label"], variant["protocol"], variant["mime"]) == ("720p", "hls", "application/x-mpegURL")
    assert variant["headers"]["Origin"] == REQUIRED_HEADERS["Origin"]
    assert variant["url"].startswith(flow.server.url + "/m/")


def test_headers_are_required_on_the_playlist_and_on_every_segment(flow):
    *_, prepared = flow.prepare(Scenario(kind="hls"))
    variant = prepared["variant"]
    playlist = variant["url"]
    segment = playlist.rsplit("/", 1)[0] + "/seg_000.ts"
    assert fetch(playlist, variant["headers"]) == 200
    assert fetch(segment, variant["headers"]) == 200
    for name in REQUIRED_HEADERS:
        partial = {k: v for k, v in variant["headers"].items() if k != name}
        assert fetch(segment, partial) == 403
    log = flow.server.state.snapshot()["requests"]
    assert [(e["file"], e["status"]) for e in log[:2]] == [("hls/index.m3u8", 200), ("hls/seg_000.ts", 200)]
    assert {e["reason"] for e in log[2:]} == {"headers"}


@pytest.mark.parametrize(("kind", "mime"), [("progressive", "video/mp4"), ("dash", "application/dash+xml")])
def test_other_protocols_reach_the_player_as_typed_variants(flow, kind, mime):
    *_, prepared = flow.prepare(Scenario(kind=kind))
    variant = prepared["variant"]
    assert (variant["protocol"], variant["mime"]) == (kind, mime)
    assert fetch(variant["url"], variant["headers"]) == 200


def test_expired_url_is_refused_and_a_refresh_keeps_the_variant(flow):
    _, _, session, prepared = flow.prepare(Scenario(kind="hls", ttl_sec=-10))
    assert fetch(prepared["variant"]["url"], prepared["variant"]["headers"]) == 403
    refreshed = flow.data("stream_refresh", session_id=prepared["session_id"], playback_session=session)
    assert refreshed["variant_changed"] is False
    assert refreshed["variant"]["identity"] == prepared["variant"]["identity"]
    assert fetch(refreshed["variant"]["url"], refreshed["variant"]["headers"]) == 200
    assert flow.server.state.snapshot()["resolves"] == 2


def test_a_refresh_may_switch_to_another_variant_that_still_fits(flow):
    _, _, session, prepared = flow.prepare(Scenario(variants=["720p", "480p"], refresh_variants=["480p"]), quality="max:720")
    assert prepared["variant"]["label"] == "720p"
    refreshed = flow.data("stream_refresh", session_id=prepared["session_id"], playback_session=session)
    assert (refreshed["variant_changed"], refreshed["variant"]["label"]) == (True, "480p")


def test_a_refresh_never_goes_past_the_quality_limit(flow):
    _, _, session, prepared = flow.prepare(Scenario(variants=["720p"], refresh_variants=["1080p"]), quality="max:720")
    error = flow.call("stream_refresh", session_id=prepared["session_id"], playback_session=session)["error"]
    assert (error["code"], error["params"]["reason"]) == ("criteria.no_match", "quality")


def test_the_debug_transport_never_leaves_the_controlled_hosts(server):
    session = controlled_host.ControlledTransport(server.url).open(SessionProfile(name="t"))
    with pytest.raises(NetworkError):
        session.get("https://example.com/")
    with pytest.raises(NetworkError):
        session.get("http://shinden.pl/series")


@pytest.mark.parametrize(("storage", "kind"), [("fs", "file"), ("content", "content_uri")])
def test_a_downloaded_episode_plays_from_the_local_target(server, tmp_path, storage, kind):
    flow = Flow(server, tmp_path, storage)
    try:
        server.state.reset(Scenario(kind="progressive"))
        series, episode = flow.listing()
        plan = flow.data(
            "queue_preview", series=series, episodes=[episode], selection={"mode": "episodes", "numbers": [episode["number"]]}
        )["plan"]
        (job,) = flow.data("queue_enqueue", plan=plan)["added"]
        flow.data("executor_start")
        deadline = time.monotonic() + 30
        while (state := flow.data("queue_job", job_id=job["job_id"])["job"]["state"]) != "completed":
            assert state not in ("failed", "cancelled") and time.monotonic() < deadline, state
            time.sleep(0.05)
        session = flow.data("playback_begin", series=series, episode=episode)["playback_session"]
        local = flow.data("playback_prepare", playback_session=session, series=series, episode=episode)
        assert (local["mode"], local["target"]["kind"]) == ("local", kind)
        if kind == "file":
            assert Path(local["target"]["value"]).read_bytes() == (MEDIA / "video.mp4").read_bytes()
        else:
            assert local["target"]["value"].startswith("content://a.b/lib/")
            relative = urllib.parse.unquote(local["target"]["value"].removeprefix("content://a.b/lib/"))
            assert (tmp_path / "lib" / relative).read_bytes() == (MEDIA / "video.mp4").read_bytes()
        downloads = [e for e in server.state.snapshot()["requests"] if e["file"] == "video.mp4"]
        assert downloads and all(e["status"] in (200, 206) for e in downloads)
    finally:
        assert json.loads(flow.bridge.shutdown(10))["data"]["ended"]


@pytest.mark.skipif(sys.platform != "linux", reason="renameat2 is a Linux system call")
def test_publishing_by_rename_never_replaces_an_existing_file(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        controlled_host.rename_noreplace(src, dst)
    assert dst.read_bytes() == b"old"
    dst.unlink()
    controlled_host.rename_noreplace(src, dst)
    assert dst.read_bytes() == b"new" and not src.exists()
