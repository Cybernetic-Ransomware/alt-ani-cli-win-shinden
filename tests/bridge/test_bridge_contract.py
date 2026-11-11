import json
import threading
from unittest.mock import patch

import pytest
from alt_ani_core.errors import NoStreamError, ShindenXhrError
from alt_ani_core.services import episode_session
from alt_ani_core.testing import FakeResponse, make_stream
from alt_ani_core.transport.api import HttpStatusError, TransportTimeout

SEARCH_HTML = "search_frieren.html"
STREAM = make_stream(
    "https://cdn.example/master.m3u8?sig=STREAMSIG",
    headers={"Referer": "https://host.example/"},
    qualities={"1080p": "https://cdn.example/1080.m3u8", "480p": "https://cdn.example/480.m3u8"},
    ext="m3u8",
)


@pytest.fixture
def extract_ok():
    with patch.object(episode_session.extract, "resolve", return_value=STREAM) as mock:
        yield mock


def _open(client) -> tuple[str, list[dict]]:
    data = client.data("open_episode", episode=client.episode)
    return data["session_id"], data["sources"]


class TestEnvelope:
    def test_success_and_error_share_one_shape(self, client):
        ok = client.call("history_watch")
        assert ok == {"ok": True, "data": {"entries": []}}

        failed = client.call("no_such_method")
        assert failed["ok"] is False
        assert set(failed["error"]) == {"code", "kind", "retryable", "message", "params"}
        assert failed["error"]["code"] == "unknown_method"

    def test_malformed_json_is_reported_not_raised(self, client):
        reply = json.loads(client.bridge.call("search", "{not json"))
        assert reply["error"]["code"] == "bad_request"

    def test_missing_field_is_bad_request(self, client):
        assert client.error("search")["code"] == "bad_request"

    def test_unknown_engine(self, client):
        assert client.error("search", engine_id="nope", query="x")["code"] == "unknown_engine"

    def test_unexpected_exception_reports_type_only(self, client):
        with patch.object(client.history, "watch_entries", side_effect=RuntimeError("https://x/?auth=SECRET")):
            error = client.error("history_watch")
        assert error["code"] == "internal"
        assert error["message"] == "RuntimeError"
        assert "SECRET" not in json.dumps(error)

    def test_invalid_backend_fails_engine_creation(self, client):
        reply = client.create(xhr_backend="bogus")
        assert reply["ok"] is False
        assert reply["error"]["code"] == "bad_request"


class TestCatalogue:
    def test_search_returns_plain_hits(self, client, tmp_path):
        client.transport.route("https://shinden.pl/series", (client.fixtures / SEARCH_HTML).read_text(encoding="utf-8"))
        hits = client.data("search", query="frieren")["hits"]
        assert hits
        assert set(hits[0]) == {"id", "slug", "title", "url", "series_type"}

    def test_network_failure_is_retryable(self, client):
        client.transport.route("https://shinden.pl/series", TransportTimeout())
        error = client.error("search", query="frieren")
        assert (error["code"], error["kind"], error["retryable"]) == ("timeout", "network", True)

    def test_http_403_is_an_access_block(self, client):
        client.transport.route("https://shinden.pl/series", FakeResponse(status_code=403))
        error = client.error("search", query="frieren")
        assert error["code"] == "access_blocked"
        assert error["params"] == {"block": "http_403"}

    def test_cloudflare_challenge_is_an_access_block(self, client):
        client.transport.route("https://shinden.pl/series", "<html><title>Just a moment...</title></html>")
        error = client.error("search", query="frieren")
        assert error["code"] == "access_blocked"
        assert error["params"]["block"] == "cloudflare_challenge"


class TestEpisodeSessions:
    def test_open_episode_returns_opaque_session_and_sources(self, client):
        session_id, sources = _open(client)
        assert len(session_id) == 32
        assert sources
        assert set(sources[0]) == {
            "online_id",
            "player",
            "lang_audio",
            "lang_subs",
            "max_res",
            "date_added",
            "subs_author",
            "source",
        }

    def test_token_never_crosses_the_bridge(self, client, extract_ok):
        session_id, sources = _open(client)
        client.data("select", session_id=session_id)
        resolved = client.data("resolve", session_id=session_id, online_id=sources[0]["online_id"])
        probed = client.data("probe_hosts", session_id=session_id)
        everything = json.dumps([sources, resolved, probed, client.events])
        assert client.page_token not in everything
        assert "SECRETEMBEDTOKEN" not in everything

    def test_select_reports_unmatched_without_widening(self, client):
        session_id, sources = _open(client)
        data = client.data("select", session_id=session_id, filter={"player_name": "no-such-host"})
        assert data["candidates"] == []
        assert data["unmatched"] == ["--player-name=no-such-host"]
        assert len(data["all_sources"]) == len(sources)

    def test_resolve_returns_variants_with_protocol_and_headers(self, client, extract_ok):
        session_id, sources = _open(client)
        data = client.data("resolve", session_id=session_id, online_id=sources[0]["online_id"], quality="480p")
        assert data["source"]["online_id"] == sources[0]["online_id"]
        assert data["embed_host"] == "host.example"
        selected = data["variants"][data["selected_index"]]
        assert selected["label"] == "480p"
        assert selected["url"] == "https://cdn.example/480.m3u8"
        assert selected["protocol"] == "hls"
        assert selected["mime"] == "application/x-mpegURL"
        assert selected["headers"] == {"Referer": "https://host.example/"}
        assert selected["height"] == 480

    def test_sources_exhausted_lists_attempts(self, client):
        session_id, sources = _open(client)
        with patch.object(episode_session.extract, "resolve", side_effect=NoStreamError("no stream")):
            error = client.error("resolve", session_id=session_id, online_id=sources[0]["online_id"])
        assert error["code"] == "sources_exhausted"
        assert error["params"]["attempts"] == [
            {"online_id": sources[0]["online_id"], "host": "host.example", "error": "NoStreamError"}
        ]

    def test_xhr_stop_carries_classified_fields_only(self, client):
        session_id, sources = _open(client)
        client.transport.route(
            lambda request: "/player_load" in request.url,
            FakeResponse(status_code=429, headers={"Retry-After": "7"}, text="secret server text"),
        )
        error = client.error("resolve", session_id=session_id, online_id=sources[0]["online_id"])
        assert error["code"] == "xhr_access_blocked"
        assert error["params"]["http_status"] == 429
        assert error["params"]["retry_after"] == 7
        assert "secret server text" not in json.dumps(error)

    def test_unknown_source_and_session(self, client):
        session_id, _sources = _open(client)
        assert client.error("resolve", session_id=session_id, online_id="0")["code"] == "unknown_source"
        assert client.error("resolve", session_id="missing", online_id="0")["code"] == "unknown_session"

    def test_force_fresh_is_bounded(self, client, extract_ok):
        session_id, sources = _open(client)
        online_id = sources[0]["online_id"]
        client.data("resolve", session_id=session_id, online_id=online_id)
        for _ in range(episode_session.MAX_FRESH_RESOLVES):
            client.data("resolve", session_id=session_id, online_id=online_id, force_fresh=True)
        error = client.error("resolve", session_id=session_id, online_id=online_id, force_fresh=True)
        assert error["code"] == "episode.fresh_resolve_limit"

    def test_reload_once_then_limit_and_reopen_resets(self, client):
        session_id, sources = _open(client)
        assert len(client.data("reload_episode", session_id=session_id)["sources"]) == len(sources)
        assert client.error("reload_episode", session_id=session_id)["code"] == "episode.reload_limit"

        reopened, _ = _open(client)
        assert reopened != session_id
        client.data("reload_episode", session_id=reopened)

    def test_close_session_forgets_it(self, client):
        session_id, _sources = _open(client)
        assert client.data("close_session", session_id=session_id) == {"closed": True}
        assert client.data("close_session", session_id=session_id) == {"closed": False}
        assert client.error("select", session_id=session_id)["code"] == "unknown_session"

    def test_set_access_drops_sessions(self, client):
        session_id, _sources = _open(client)
        dropped = client.data("set_access", access={"use_flaresolverr": False})
        assert dropped == {"dropped_sessions": 1}
        assert client.error("select", session_id=session_id)["code"] == "unknown_session"
        assert client.data("engine_status")["sessions"] == 0


class TestOperations:
    def _blocked_show(self, client) -> tuple[threading.Event, threading.Event]:
        entered, release = threading.Event(), threading.Event()

        def show(request):
            entered.set()
            assert release.wait(5)
            return '<iframe src="//host.example/e/abc"></iframe>'

        client.transport.route(lambda request: "/player_show" in request.url, show)
        return entered, release

    def _resolve_in_thread(self, client, session_id: str, online_id: str, op_id: str) -> tuple[threading.Thread, list]:
        replies: list[dict] = []
        thread = threading.Thread(
            target=lambda: replies.append(client.call("resolve", session_id=session_id, online_id=online_id, op_id=op_id))
        )
        thread.start()
        return thread, replies

    def test_cancel_unknown_operation_is_false(self, client):
        assert client.bridge.cancel("never-started") is False

    def test_cancel_running_operation(self, client, extract_ok):
        session_id, sources = _open(client)
        entered, release = self._blocked_show(client)
        thread, replies = self._resolve_in_thread(client, session_id, sources[0]["online_id"], "op-1")
        assert entered.wait(5)
        assert client.data("engine_status")["running_operations"] == 1

        assert client.bridge.cancel("op-1") is True
        release.set()
        thread.join(5)

        assert replies[0]["error"]["code"] == "cancelled"
        extract_ok.assert_not_called()
        assert client.bridge.cancel("op-1") is False
        assert client.data("engine_status")["running_operations"] == 0

    def test_operation_id_cannot_be_reused_while_running(self, client, extract_ok):
        session_id, sources = _open(client)
        entered, release = self._blocked_show(client)
        thread, replies = self._resolve_in_thread(client, session_id, sources[0]["online_id"], "op-1")
        assert entered.wait(5)

        assert client.error("history_watch", op_id="op-1")["code"] == "operation_id_in_use"
        # the rejected duplicate must not unregister the running operation
        assert client.bridge.cancel("op-1") is True
        release.set()
        thread.join(5)
        assert replies[0]["error"]["code"] == "cancelled"

    def test_finished_operation_id_can_be_reused(self, client):
        client.data("history_watch", op_id="op-1")
        client.data("history_watch", op_id="op-1")

    def test_close_engine_cancels_operations_and_forgets_sessions(self, client, extract_ok):
        session_id, sources = _open(client)
        entered, release = self._blocked_show(client)
        thread, replies = self._resolve_in_thread(client, session_id, sources[0]["online_id"], "op-1")
        assert entered.wait(5)

        closed = client.close()
        release.set()
        thread.join(5)

        assert closed == {"ok": True, "data": {"cancelled_operations": 1, "closed_sessions": 1}}
        assert replies[0]["error"]["code"] in {"cancelled", "stale_session"}
        extract_ok.assert_not_called()
        assert client.error("select", session_id=session_id)["code"] == "unknown_engine"
        assert client.close()["error"]["code"] == "unknown_engine"
        assert all(session.closed for session in client.transport.sessions)


class TestEvents:
    def test_events_arrive_as_typed_json(self, client, extract_ok):
        session_id, sources = _open(client)
        client.data("resolve", session_id=session_id, online_id=sources[0]["online_id"])
        types = [event["type"] for event in client.events]
        assert types[:2] == ["PhaseStarted", "PhaseFinished"]
        assert {"type": "PhaseStarted", "phase": "antibot_delay"} in client.events

    def test_listener_object_with_on_event(self, client, extract_ok):
        received: list[str] = []

        class Listener:
            def onEvent(self, message: str) -> None:  # noqa: N802
                received.append(message)

        client.create(listener=Listener())
        session_id, sources = _open(client)
        client.data("resolve", session_id=session_id, online_id=sources[0]["online_id"])
        assert received
        assert all(isinstance(json.loads(message)["type"], str) for message in received)

    def test_failing_listener_does_not_break_the_operation(self, client, extract_ok):
        def broken(_message: str) -> None:
            raise RuntimeError("host callback crashed")

        client.create(listener=broken)
        session_id, sources = _open(client)
        data = client.data("resolve", session_id=session_id, online_id=sources[0]["online_id"])
        assert data["variants"]
        assert client.data("engine_status")["listener_errors"] >= 2

    def test_no_listener_is_allowed(self, client, extract_ok):
        reply = json.loads(
            client.bridge.create_engine(
                json.dumps({"cache_dir": str(client.tmp_path / "c"), "history_path": str(client.tmp_path / "h.db")})
            )
        )
        assert reply["ok"]
        client.bridge.close_engine(json.dumps(reply["data"]))


class TestHistory:
    SERIES = {"id": "61064", "slug": "sousou-no-frieren", "title": "Frieren", "url": "https://shinden.pl/series/61064"}

    def test_watch_and_download_round_trip(self, client):
        client.data("history_record_watch", series=self.SERIES, episode=5.5)
        client.data("history_record_download", series=self.SERIES, episode=1)
        client.data("history_record_download", series=self.SERIES, episode=2)

        watch = client.data("history_watch")["entries"]
        downloads = client.data("history_downloads")["entries"]
        assert watch[0]["series"] == self.SERIES
        assert watch[0]["last_ep"] == 5.5
        assert downloads[0]["episodes"] == [1.0, 2.0]

        client.data("history_clear")
        assert client.data("history_watch")["entries"] == []


class TestPlatformPieces:
    def test_missing_transport_is_reported(self, tmp_path):
        from alt_ani_bridge import Bridge
        from alt_ani_bridge import bridge as bridge_module

        with patch.dict("sys.modules", {"alt_ani_core.transport.curl": None}):
            reply = json.loads(
                Bridge(transport_factory=bridge_module._default_transport).create_engine(
                    json.dumps({"cache_dir": str(tmp_path), "history_path": str(tmp_path / "h.db")})
                )
            )
        assert reply["error"]["code"] == "transport_unavailable"

    def test_http_status_other_than_403(self):
        from alt_ani_bridge.errors import describe

        assert describe(HttpStatusError(503))["retryable"] is True
        assert describe(HttpStatusError(404))["retryable"] is False

    def test_xhr_error_mapping_uses_outcome(self):
        from alt_ani_bridge.errors import describe

        err = ShindenXhrError("x", stage="player_show", category="timeout", reason="transport")
        described = describe(err)
        assert described["code"] == "xhr_service_unavailable"
        assert described["retryable"] is True
