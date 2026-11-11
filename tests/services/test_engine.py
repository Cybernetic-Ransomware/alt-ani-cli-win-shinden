import threading
from unittest.mock import MagicMock

import pytest
from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import EngineClosedError, OperationCancelled, ParseError, StaleSessionError
from alt_ani_core.events import ClearanceRequested, ClearanceUnavailable, EmbedResolved
from alt_ani_core.models import FallbackPolicy, SeriesRef
from alt_ani_core.rules.health import HostState
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.testing import FakeResponse, RecordingCancel
from alt_ani_core.transport import clearance
from alt_ani_core.transport.clearance import Clearance

from .support import (
    EPISODE,
    EPISODE_URL,
    OTHER_TOKEN,
    TIMEOUT,
    TRAP_ID,
    Background,
    Gate,
    embed_host,
    iframe,
    is_load,
    is_show,
    xhr_calls,
)

_DIRECT = AccessConfig(use_flaresolverr=False)
_SERIES_URL = "https://shinden.pl/series/61064-sousou-no-frieren"
_REF = SeriesRef(id="61064", slug="sousou-no-frieren", title="Frieren", url=_SERIES_URL)


def _retire(engine, how: str) -> None:
    if how == "close":
        engine.close()
    else:
        engine.set_access(_DIRECT)


@pytest.fixture
def clearance_lookups(monkeypatch):
    """Both clearance sources as mocks that yield nothing unless a test sets a return value."""
    load = MagicMock(name="load_cached", return_value=None)
    fetch = MagicMock(name="fetch_via_flaresolverr", return_value=None)
    monkeypatch.setattr(clearance, "load_cached", load)
    monkeypatch.setattr(clearance, "fetch_via_flaresolverr", fetch)
    return load, fetch


@pytest.mark.unit
class TestLifecycle:
    def test_http_session_is_opened_lazily_and_shared_by_operations(self, make_engine, transport, episode_page, shinden_html):
        transport.route(EPISODE_URL, episode_page())
        transport.route(_SERIES_URL, shinden_html("series_frieren.html"))
        engine = make_engine()
        assert transport.sessions == []

        engine.open_episode(EPISODE)
        engine.metadata(_REF)

        assert len(transport.sessions) == 1
        assert transport.sessions[0].profile.name == "shinden"
        assert transport.sessions[0].closed is False
        assert {request.profile for request in transport.requests} == {"shinden"}

    def test_search_goes_through_the_shinden_session(self, make_engine, transport, shinden_html):
        transport.route("https://shinden.pl/series", shinden_html("search_frieren.html"))
        engine = make_engine()

        hits = engine.search("frieren")

        assert hits
        assert transport.requests[0].params == {"search": "frieren"}
        assert transport.requests[0].profile == "shinden"

    def test_series_from_url_needs_no_session(self, make_engine, transport):
        engine = make_engine()

        ref = engine.series_from_url(_SERIES_URL)

        assert (ref.id, ref.slug) == ("61064", "sousou-no-frieren")
        assert transport.sessions == []

    def test_operations_after_close_raise_engine_closed(self, opened, transport, extract_seam):
        engine, session = opened()
        engine.close()
        before = len(transport.requests)

        assert engine.closed is True
        for call in (
            lambda: engine.search("frieren"),
            lambda: engine.metadata(_REF),
            lambda: engine.metadata_many([_REF]),
            lambda: engine.episodes(_REF),
            lambda: engine.open_episode(EPISODE),
            lambda: engine.set_access(_DIRECT),
            lambda: session.resolve(session.sources[0]),
            lambda: session.probe_hosts(session.sources),
            session.reload,
        ):
            with pytest.raises(EngineClosedError):
                call()
        assert len(transport.requests) == before
        assert len(transport.sessions) == 1

    def test_close_is_idempotent_and_closes_the_session(self, opened, transport):
        engine, _session = opened()
        generation = engine.generation

        engine.close()
        engine.close()

        assert transport.sessions[0].closed is True
        assert engine.generation == generation + 1

    def test_close_before_any_operation_opens_nothing(self, make_engine, transport):
        engine = make_engine()

        engine.close()
        engine.close()

        assert transport.sessions == []
        with pytest.raises(EngineClosedError):
            engine.search("x")

    def test_context_manager_closes_the_engine(self, make_engine, transport, episode_page):
        transport.route(EPISODE_URL, episode_page())

        with make_engine() as engine:
            engine.open_episode(EPISODE)

        assert engine.closed is True
        assert transport.sessions[0].closed is True

    def test_open_episode_always_returns_a_new_session(self, opened):
        engine, session = opened()

        again = engine.open_episode(EPISODE)

        assert isinstance(again, EpisodeSession)
        assert again is not session
        assert again.episode is EPISODE
        assert again.sources == session.sources


@pytest.mark.unit
class TestClearance:
    def test_direct_only_policy_looks_up_no_clearance(self, make_engine, transport, sink, episode_page, clearance_lookups):
        load, fetch = clearance_lookups
        load.return_value = Clearance("CACHED_CF", "UA/cached")
        transport.route(EPISODE_URL, episode_page())
        engine = make_engine(access=AccessConfig(flaresolverr_url="http://solver.local:8191", use_flaresolverr=False))

        engine.open_episode(EPISODE)

        load.assert_not_called()
        fetch.assert_not_called()
        assert sink.of(ClearanceRequested) == [] and sink.of(ClearanceUnavailable) == []
        profile = transport.sessions[0].profile
        assert "cf_clearance" not in profile.cookies
        assert "User-Agent" not in profile.headers

    def test_solver_url_emits_clearance_requested_and_uses_the_result(
        self, make_engine, transport, sink, episode_page, clearance_lookups, tmp_path
    ):
        load, fetch = clearance_lookups
        fetch.return_value = Clearance("SOLVED_CF", "UA/solved")
        transport.route(EPISODE_URL, episode_page())
        engine = make_engine(access=AccessConfig(flaresolverr_url="http://solver.local:8191"))

        engine.open_episode(EPISODE)

        fetch.assert_called_once_with("http://solver.local:8191", tmp_path / "cache")
        assert sink.of(ClearanceRequested) == [ClearanceRequested()]
        assert sink.of(ClearanceUnavailable) == []
        profile = transport.sessions[0].profile
        assert profile.cookies["cf_clearance"] == "SOLVED_CF"
        assert profile.headers["User-Agent"] == "UA/solved"
        assert "SOLVED_CF" not in repr(profile) + repr(sink.events)

    def test_solver_yielding_nothing_is_reported_and_the_engine_goes_direct(
        self, make_engine, transport, sink, episode_page, clearance_lookups
    ):
        _load, fetch = clearance_lookups
        transport.route(EPISODE_URL, episode_page())
        engine = make_engine(access=AccessConfig(flaresolverr_url="http://solver.local:8191"))

        session = engine.open_episode(EPISODE)

        fetch.assert_called_once()
        assert sink.events[:2] == [ClearanceRequested(), ClearanceUnavailable()]
        assert len(session.sources) == 9
        assert len(transport.sessions) == 1
        assert "cf_clearance" not in transport.sessions[0].profile.cookies

    def test_cached_clearance_skips_the_solver(self, make_engine, transport, sink, episode_page, clearance_lookups):
        load, fetch = clearance_lookups
        load.return_value = Clearance("CACHED_CF", "UA/cached")
        transport.route(EPISODE_URL, episode_page())
        engine = make_engine(access=AccessConfig(flaresolverr_url="http://solver.local:8191"))

        engine.open_episode(EPISODE)

        fetch.assert_not_called()
        assert sink.of(ClearanceRequested) == []
        assert transport.sessions[0].profile.cookies["cf_clearance"] == "CACHED_CF"

    def test_no_solver_url_means_no_solver_call_and_no_event(
        self, make_engine, transport, sink, episode_page, clearance_lookups
    ):
        load, fetch = clearance_lookups
        transport.route(EPISODE_URL, episode_page())
        engine = make_engine()

        engine.open_episode(EPISODE)

        load.assert_called_once()
        fetch.assert_not_called()
        assert sink.of(ClearanceRequested) == [] and sink.of(ClearanceUnavailable) == []

    def test_clearance_is_looked_up_once_per_http_session(self, make_engine, transport, sink, episode_page, clearance_lookups):
        _load, fetch = clearance_lookups
        transport.route(EPISODE_URL, episode_page())
        engine = make_engine(access=AccessConfig(flaresolverr_url="http://solver.local:8191"))

        engine.open_episode(EPISODE)
        engine.open_episode(EPISODE)
        assert fetch.call_count == 1

        engine.set_access(AccessConfig(flaresolverr_url="http://other.local:8191"))
        assert fetch.call_count == 1
        engine.open_episode(EPISODE)

        assert fetch.call_count == 2
        assert fetch.call_args.args[0] == "http://other.local:8191"
        assert len(sink.of(ClearanceRequested)) == 2


@pytest.mark.unit
class TestAccessChange:
    def test_set_access_retires_the_idle_session_and_reopens_lazily(self, opened, transport, clearance_lookups):
        load, _fetch = clearance_lookups
        engine, _session = opened()
        old = transport.sessions[0]
        generation = engine.generation
        load.return_value = Clearance("NEW_CF", "UA/new")

        engine.set_access(AccessConfig())

        assert old.closed is True
        assert engine.generation == generation + 1
        assert len(transport.sessions) == 1
        engine.open_episode(EPISODE)
        assert len(transport.sessions) == 2
        new = transport.sessions[1]
        assert new.closed is False
        assert new.profile is not old.profile
        assert "cf_clearance" not in old.profile.cookies
        assert new.profile.cookies["cf_clearance"] == "NEW_CF"
        assert new.profile.headers["User-Agent"] == "UA/new"

    def test_sessions_opened_before_set_access_are_stale_for_every_operation(self, opened, transport, extract_seam, diag):
        engine, session = opened()
        a = session.sources[0]
        assert session.probe_hosts([a]) is None
        engine.set_access(_DIRECT)
        before = len(transport.requests)

        for call in (
            lambda: session.resolve(a),
            lambda: session.resolve(a, candidates=session.sources, force_fresh=True),
            lambda: session.probe_hosts(session.sources),
            session.reload,
        ):
            with pytest.raises(StaleSessionError):
                call()

        assert len(transport.requests) == before
        extract_seam.assert_not_called()
        diag.resolve_result.assert_not_called()

    def test_open_episode_after_set_access_is_a_new_working_session(self, opened, transport, extract_seam):
        engine, session = opened()
        a = session.sources[0]
        session.resolve(a)
        engine.set_access(_DIRECT)

        again = engine.open_episode(EPISODE)

        assert again is not session
        assert again.known_sources == {}
        assert again.resolve(a).source is a
        assert again.reload_used is False
        with pytest.raises(StaleSessionError):
            session.resolve(a)
        assert [s.closed for s in transport.sessions] == [True, False]


@pytest.mark.unit
class TestRetireDuringOperation:
    """set_access()/close() while an operation is blocked in a request: its result is discarded, nothing is recorded."""

    @pytest.mark.parametrize("how", ["set_access", "close"])
    @pytest.mark.parametrize("stage", ["player_load", "player_show"])
    def test_resolve_blocked_in_an_xhr_request(self, opened, transport, extract_seam, diag, sink, how, stage):
        engine, session = opened()
        a = session.sources[0]
        gate = Gate()
        if stage == "player_load":
            transport.route(is_load, lambda _request: (gate.block(), "0")[1])
        else:
            transport.route(is_show, lambda request: (gate.block(), iframe(request))[1])
        old = transport.sessions[0]

        op = Background("op", lambda: session.resolve(a, candidates=session.sources, policy=FallbackPolicy.WALK))
        gate.wait_entered()
        _retire(engine, how)
        still_open_while_in_flight = old.closed is False
        gate.release.set()
        op.join()

        assert isinstance(op.error, StaleSessionError)
        assert op.result is None
        assert still_open_while_in_flight
        assert old.closed is True
        assert {online_id for online_id, _stage in xhr_calls(transport)} == {a.online_id}
        assert xhr_calls(transport)[-1] == (a.online_id, stage)
        extract_seam.assert_not_called()
        diag.resolve_result.assert_not_called()
        diag.host_health.assert_not_called()
        assert engine.health.state(embed_host(a.online_id)) is HostState.UNKNOWN
        assert session.known_sources == {}
        assert sink.of(EmbedResolved) == []
        assert len(transport.sessions) == 1

    @pytest.mark.parametrize("how", ["set_access", "close"])
    def test_resolve_blocked_in_extraction(self, opened, transport, extract_seam, diag, how):
        engine, session = opened()
        a = session.sources[0]
        gate = Gate()
        extract_seam.side_effect = lambda *_a, **_k: (gate.block(), extract_seam.return_value)[1]
        old = transport.sessions[0]

        op = Background("op", lambda: session.resolve(a))
        gate.wait_entered()
        _retire(engine, how)
        still_open_while_in_flight = old.closed is False
        gate.release.set()
        op.join()

        assert isinstance(op.error, StaleSessionError)
        assert op.result is None
        assert still_open_while_in_flight
        assert old.closed is True
        diag.resolve_result.assert_not_called()
        diag.host_health.assert_not_called()
        assert engine.health.state(embed_host(a.online_id)) is HostState.UNKNOWN

    @pytest.mark.parametrize("how", ["set_access", "close"])
    def test_probe_blocked_in_a_request_learns_nothing_from_it(self, opened, transport, how):
        engine, session = opened()
        a, b = session.sources[:2]
        gate = Gate()
        transport.route(is_show, lambda request: (gate.block() if b.online_id in request.url else None, iframe(request))[1])

        op = Background("op", lambda: session.probe_hosts(session.sources))
        gate.wait_entered()
        _retire(engine, how)
        gate.release.set()
        op.join()

        assert isinstance(op.error, StaleSessionError)
        assert set(session.known_sources) == {a.online_id}
        assert xhr_calls(transport)[-1] == (b.online_id, "player_show")
        assert transport.sessions[0].closed is True

    @pytest.mark.parametrize("how", ["set_access", "close"])
    def test_reload_blocked_in_the_page_request_keeps_the_old_state(self, opened, transport, episode_page, how):
        engine, session = opened()
        a = session.sources[0]
        assert session.probe_hosts([a]) is None
        sources = session.sources
        gate = Gate()
        transport.route(EPISODE_URL, lambda _request: (gate.block(), episode_page(OTHER_TOKEN).replace(TRAP_ID, "1825161"))[1])

        op = Background("op", session.reload)
        gate.wait_entered()
        _retire(engine, how)
        gate.release.set()
        op.join()

        assert isinstance(op.error, StaleSessionError)
        assert session.sources == sources
        assert set(session.known_sources) == {a.online_id}

    @pytest.mark.parametrize("how", ["set_access", "close"])
    def test_open_episode_blocked_in_the_page_request_returns_no_session(self, make_engine, transport, episode_page, how):
        gate = Gate()
        transport.route(EPISODE_URL, lambda _request: (gate.block(), episode_page())[1])
        engine = make_engine()

        op = Background("op", lambda: engine.open_episode(EPISODE))
        gate.wait_entered()
        old = transport.sessions[0]
        _retire(engine, how)
        still_open_while_in_flight = old.closed is False
        gate.release.set()
        op.join()

        assert isinstance(op.error, StaleSessionError)
        assert op.result is None
        assert still_open_while_in_flight
        assert old.closed is True

    def test_next_operation_after_set_access_gets_a_new_session_with_a_fresh_profile(
        self, opened, transport, extract_seam, clearance_lookups
    ):
        load, _fetch = clearance_lookups
        engine, session = opened()
        a = session.sources[0]
        gate = Gate()
        transport.route(is_load, lambda _request: (gate.block(), "0")[1] if not gate.release.is_set() else "0")
        old = transport.sessions[0]

        op = Background("op", lambda: session.resolve(a))
        gate.wait_entered()
        load.return_value = Clearance("NEW_CF", "UA/new")
        engine.set_access(AccessConfig())
        gate.release.set()
        op.join()
        assert isinstance(op.error, StaleSessionError)
        assert len(transport.sessions) == 1

        again = engine.open_episode(EPISODE)
        resolved = again.resolve(a)

        assert resolved.source is a
        assert len(transport.sessions) == 2
        new = transport.sessions[1]
        assert (old.closed, new.closed) == (True, False)
        assert new.profile is not old.profile
        assert new.profile.cookies["cf_clearance"] == "NEW_CF"
        assert "cf_clearance" not in old.profile.cookies
        assert again is not session
        with pytest.raises(StaleSessionError):
            session.resolve(a)

    def test_two_leases_keep_the_retired_session_open_until_both_are_released(self, opened, transport, shinden_html):
        engine, session = opened()
        a = session.sources[0]
        first_gate, second_gate = Gate(), Gate()
        transport.route(is_load, lambda _request: (first_gate.block(), "0")[1])
        transport.route(_SERIES_URL, lambda _request: (second_gate.block(), shinden_html("series_frieren.html"))[1])
        old = transport.sessions[0]

        first = Background("first", lambda: session.resolve(a))
        second = Background("second", lambda: engine.metadata(_REF))
        first_gate.wait_entered()
        second_gate.wait_entered()
        engine.set_access(_DIRECT)
        first_gate.release.set()
        first.join()
        open_with_one_lease_left = old.closed is False
        second_gate.release.set()
        second.join()

        assert isinstance(first.error, StaleSessionError)
        assert isinstance(second.error, StaleSessionError)
        assert open_with_one_lease_left
        assert old.closed is True


def _ref(number: int) -> SeriesRef:
    return SeriesRef(
        id=str(number), slug=f"series-{number}", title=f"Series {number}", url=f"https://shinden.pl/series/{number}-s"
    )


@pytest.mark.unit
class TestMetadataMany:
    _PAGES = ("series_frieren.html", "series_announcement.html", "series_music_video.html")

    @pytest.fixture
    def refs(self) -> list[SeriesRef]:
        return [_ref(101), _ref(102), _ref(103)]

    @pytest.fixture
    def bodies(self, refs, shinden_html) -> dict[str, str]:
        return {ref.url: shinden_html(page) for ref, page in zip(refs, self._PAGES, strict=True)}

    @pytest.fixture
    def all_at_once(self, transport, refs, bodies):
        """Each reply waits until every worker is inside a request, so the pool cannot reuse one thread."""
        barrier = threading.Barrier(len(refs), timeout=TIMEOUT)

        def reply(request):
            barrier.wait()
            return bodies[request.url]

        for ref in refs:
            transport.route(ref.url, reply)
        return barrier

    def _expected(self, engine, transport, refs, bodies):
        for ref in refs:
            transport.route(ref.url, bodies[ref.url])
        return [engine.metadata(ref) for ref in refs]

    def test_empty_input_needs_no_session(self, make_engine, transport):
        assert make_engine().metadata_many([]) == []
        assert transport.sessions == []

    def test_every_worker_thread_uses_its_own_session(self, make_engine, transport, refs, bodies, all_at_once):
        engine = make_engine()

        results = engine.metadata_many(refs)

        main, workers = transport.sessions[0], transport.sessions[1:]
        assert len(workers) == len(refs)
        assert all(len(worker.threads) == 1 for worker in workers)
        thread_ids = [next(iter(worker.threads)) for worker in workers]
        assert len(set(thread_ids)) == len(refs)
        assert threading.get_ident() not in thread_ids
        assert main.threads == set()
        assert all(worker.profile == main.profile for worker in workers)
        assert {request.profile for request in transport.requests} == {"shinden"}
        assert all(worker.closed for worker in workers)
        assert main.closed is False
        assert sorted(transport.urls) == sorted(ref.url for ref in refs)
        assert None not in results

    def test_results_follow_the_input_order(self, make_engine, transport, refs, bodies, all_at_once):
        engine = make_engine()

        results = engine.metadata_many(refs)
        reversed_results = engine.metadata_many(refs[::-1])

        expected = self._expected(engine, transport, refs, bodies)
        assert len(set(expected)) == len(refs)
        assert results == expected
        assert reversed_results == expected[::-1]

    @pytest.mark.parametrize(
        "broken",
        [FakeResponse(status_code=500), "<html><body>not a series page</body></html>"],
        ids=["http-500", "unreadable-page"],
    )
    def test_failing_page_yields_none_for_that_entry_only(self, make_engine, transport, refs, bodies, broken):
        engine = make_engine()
        expected = self._expected(engine, transport, refs, bodies)
        transport.route(refs[1].url, broken)

        results = engine.metadata_many(refs)

        assert results == [expected[0], None, expected[2]]
        assert all(worker.closed for worker in transport.sessions[1:])

    def test_cancellation_propagates_and_closes_worker_sessions(self, make_engine, transport, refs, bodies, all_at_once):
        engine = make_engine()
        cancel = CancelToken()

        def cancelling(request):
            all_at_once.wait()
            cancel.cancel()
            return bodies[request.url]

        transport.route(refs[1].url, cancelling)

        with pytest.raises(OperationCancelled):
            engine.metadata_many(refs, cancel=cancel)

        main, workers = transport.sessions[0], transport.sessions[1:]
        assert len(workers) == len(refs)
        assert all(worker.closed for worker in workers)
        assert main.closed is False
        assert engine.closed is False

    def test_token_cancelled_beforehand_opens_nothing(self, make_engine, transport, refs):
        cancel = CancelToken()
        cancel.cancel()

        with pytest.raises(OperationCancelled):
            make_engine().metadata_many(refs, cancel=cancel)

        assert transport.sessions == [] and transport.requests == []

    @pytest.mark.parametrize("how", ["set_access", "close"])
    def test_staleness_propagates_and_closes_every_session(self, make_engine, transport, refs, bodies, all_at_once, how):
        engine = make_engine()

        def retiring(request):
            all_at_once.wait()
            _retire(engine, how)
            return bodies[request.url]

        transport.route(refs[1].url, retiring)

        with pytest.raises(StaleSessionError):
            engine.metadata_many(refs)

        assert len(transport.sessions) == 1 + len(refs)
        assert all(session.closed for session in transport.sessions)
        assert transport.sessions[0].threads == set()
