"""The whole MVP path through JSON only, against a local HTTP server; restart and reconnect variants."""

import json
import time

import pytest
from offline.servers import Resource, ServerState, random_body, serve

from alt_ani_core.transfer import transfer

from .conftest import FIXTURES, BridgeSources, make_v2, player, progressive

pytestmark = pytest.mark.loopback


def _client(tmp_path, url: str):
    sources = BridgeSources([player("1")], {"1": lambda fresh: progressive(f"{url}/v.mp4")})
    client = make_v2(tmp_path, sources=sources, transfer=transfer)
    client.transport.route("https://shinden.pl/series", (FIXTURES / "search_frieren.html").read_text(encoding="utf-8"))
    client.transport.route(lambda r: "/episodes" in r.url, (FIXTURES / "episodes_frieren.html").read_text(encoding="utf-8"))
    assert client.create(push=False, storage={"kind": "fs", "root": str(tmp_path / "library")})["ok"]
    return client


def test_search_to_offline_playback_export_and_delete(tmp_path):
    body = random_body(300_000)
    with serve(ServerState(resources={"/v.mp4": Resource(body, '"e1"')})) as url:
        client = _client(tmp_path, url)
        hit = client.data("search", query="frieren")["hits"][0]
        listing = client.data("episodes", series={k: hit[k] for k in ("id", "slug", "title", "url")})
        series = listing["series"]
        first_two = listing["episodes"][:2]
        preview = client.data(
            "queue_preview", series=series, episodes=first_two, selection={"mode": "unwatched"}, criteria={"quality": "best"}
        )
        cursor = client.head()
        added = client.data("queue_enqueue", plan=preview["plan"])["added"]
        assert len(added) == 2
        client.data("executor_start")
        for job in added:
            client.wait_job(job["job_id"], "completed", timeout=30)
        event, cursor = client.wait_event(cursor, lambda e: e["type"] == "job.changed" and e["data"]["bytes_done"])
        assert event["data"]["job_id"] in {j["job_id"] for j in added}
        items = client.data("library_list", series_id=series["id"])["items"]
        assert len(items) == 2
        episode = first_two[0]
        session = client.data("playback_begin", series=series, episode=episode)["playback_session"]
        local = client.data("playback_prepare", playback_session=session, series=series, episode=episode)
        assert local["mode"] == "local"
        with open(local["target"]["value"], "rb") as fh:
            assert fh.read() == body
        client.data("playback_position", playback_session=session, seq=1, position_ms=4200)
        exported = client.data(
            "library_export", item_ids=[items[0]["item_id"]], target={"kind": "directory", "path": str(tmp_path / "out")}
        )
        assert (tmp_path / "out" / exported["results"][0]["file_name"]).read_bytes() == body
        client.data("library_delete", item_ids=[items[0]["item_id"]])
        watch = client.data("watch_get", series_id=series["id"], numbers=[episode["number"]])["episodes"][0]
        assert watch["position_ms"] == 4200 and watch["opened_via"] == "player"
        assert (tmp_path / "out" / exported["results"][0]["file_name"]).exists()
        assert json.loads(client.bridge.shutdown(10))["data"]["ended"]


def test_restart_during_transfer_recovers_and_finishes(tmp_path):
    body = random_body(2_000_000)
    state = ServerState(resources={"/v.mp4": Resource(body, '"e1"')}, stall_after=200_000)
    with serve(state) as url:
        client = _client(tmp_path, url)
        (job,) = client.enqueue((1,))
        client.data("executor_start")
        for _ in range(1500):
            if (client.job(job["job_id"])["bytes_done"] or 0) > 0:
                break
            time.sleep(0.01)
        assert json.loads(client.bridge.shutdown(10))["data"]["ended"]
        state.stall.set()
        again = _client(tmp_path, url)
        again.data("executor_start")
        assert again.wait_job(job["job_id"], "completed", timeout=30)["state"] == "completed"
        (item,) = again.data("library_list")["items"]
        assert item["size_bytes"] == len(body)
        json.loads(again.bridge.shutdown(10))


def test_client_reconnecting_midway_resynchronises(tmp_path):
    with serve(ServerState(resources={"/v.mp4": Resource(random_body(100_000), '"e1"')})) as url:
        client = _client(tmp_path, url)
        jobs = client.enqueue((1, 2))
        client.data("executor_start")
        client.wait_job(jobs[0]["job_id"], "completed", timeout=30)
        # a second UI handle joins later: head, snapshots, then the cursor
        from .conftest import V2Client

        late = V2Client(client.bridge, client.transport, None, tmp_path)
        late.create(push=False)
        head = late.head()
        known = {j["job_id"]: (j["revision"], j["state"]) for j in late.data("queue_snapshot")["jobs"]}
        cursor = head
        while any(state != "completed" for _, state in known.values()):
            events, cursor = late.drain(cursor, wait_ms=200)
            for event in events:
                if event["type"] == "job.changed" and event["data"]["revision"] > known[event["data"]["job_id"]][0]:
                    job = late.job(event["data"]["job_id"])
                    known[job["job_id"]] = (job["revision"], job["state"])
        assert all(state == "completed" for _, state in known.values())
        json.loads(client.bridge.shutdown(10))
