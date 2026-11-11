import pytest
from alt_ani_core.errors import (
    AntiBotError,
    FreshResolveLimitError,
    NoStreamError,
    OperationCancelled,
    ReloadLimitError,
    ShindenXhrError,
    SourcesExhaustedError,
)
from alt_ani_core.events import PhaseFinished, PhaseStarted, SourceAttemptFailed
from alt_ani_core.models import ExtractionFailure, FallbackPolicy, PlayerEntry, PlayerSource, Purpose, SourceFilter
from alt_ani_core.rules.health import HostState, Signal
from alt_ani_core.services.episode_session import MAX_FRESH_RESOLVES
from alt_ani_core.shinden.api import XhrOutcome, xhr_outcome
from alt_ani_core.testing import FakeResponse, RecordingCancel, RecordingSink

from .support import (
    EPISODE,
    EPISODE_URL,
    OTHER_TOKEN,
    QUIET,
    TEST_TOKEN,
    TRAP_ID,
    Background,
    Gate,
    Traffic,
    embed_host,
    embed_url,
    iframe,
    invalid_token,
    is_load,
    is_show,
    online_id_of,
    page_requests,
    quoted,
    xhr_calls,
    xhr_urls,
)

WALK = FallbackPolicy.WALK

_HARD_FAILURE = ExtractionFailure(
    layer="custom", category="network_error", used_fallback=True, fallback_layer="ytdlp", fallback_category="network_error"
)


def _hard_no_stream() -> NoStreamError:
    err = NoStreamError("host is down")
    err.failure = _HARD_FAILURE
    return err


def _ids(players) -> list[str]:
    return [p.online_id for p in players]


@pytest.mark.unit
class TestSerialization:
    """A second thread enters a session operation only after the first one left it."""

    @pytest.fixture
    def install_traffic(self, transport, episode_page):
        """Call after opening the session: later routes win, and these must win over the defaults."""

        def _install() -> Traffic:
            traffic = Traffic()
            self.gate = Gate()
            self.gated_show: str | None = None

            def show(request):
                if online_id_of(request.url) == self.gated_show:
                    self.gate.block()
                return iframe(request)

            transport.route(EPISODE_URL, traffic.wrap(episode_page()))
            transport.route(is_load, traffic.wrap("0"))
            transport.route(is_show, traffic.wrap(show))
            return traffic

        return _install

    @pytest.mark.parametrize("second_op", ["resolve", "probe_hosts", "reload"])
    def test_second_operation_waits_for_a_resolve_blocked_in_a_request(self, opened, extract_seam, install_traffic, second_op):
        _engine, session = opened()
        traffic = install_traffic()
        a, b = session.sources[:2]
        self.gated_show = a.online_id
        calls = {
            "resolve": lambda: session.resolve(b),
            "probe_hosts": lambda: session.probe_hosts([b]),
            "reload": session.reload,
        }

        first = Background("first", lambda: session.resolve(a))
        self.gate.wait_entered()
        second = Background("second", calls[second_op])
        assert not second.done.wait(QUIET)
        assert traffic.by("second") == []
        self.gate.release.set()
        first.join()
        second.join()

        assert first.error is None and second.error is None
        assert first.result.source is a
        names = [name for name in traffic.threads() if name in ("first", "second")]
        assert names == sorted(names) and "second" in names
        assert traffic.max_active == 1
        if second_op == "reload":
            assert session.known_sources == {}
            assert session.reload_used is True
        else:
            assert set(session.known_sources) == {a.online_id, b.online_id}

    def test_resolve_waits_for_a_reload_blocked_in_the_page_request(self, opened, transport, episode_page, extract_seam):
        _engine, session = opened()
        a = session.sources[0]
        assert session.probe_hosts([a]) is None
        traffic, gate = Traffic(), Gate()

        def page(_request):
            gate.block()
            return episode_page(OTHER_TOKEN)

        transport.route(EPISODE_URL, traffic.wrap(page))
        transport.route(is_load, traffic.wrap("0"))
        transport.route(is_show, traffic.wrap(iframe))

        first = Background("first", session.reload)
        gate.wait_entered()
        second = Background("second", lambda: session.resolve(a))
        assert not second.done.wait(QUIET)
        assert traffic.by("second") == []
        gate.release.set()
        first.join()
        second.join()

        assert first.error is None and second.error is None
        # the resolve ran on the reloaded state: the old cache entry was gone and the new token was used
        assert len(traffic.by("second")) == 2
        assert all(quoted(OTHER_TOKEN) in url for url in traffic.by("second"))
        assert set(session.known_sources) == {a.online_id}
        assert traffic.max_active == 1

    def test_two_probes_never_overlap_and_the_second_skips_what_the_first_learned(self, opened, install_traffic):
        _engine, session = opened()
        traffic = install_traffic()
        a, b, c = session.sources[:3]
        self.gated_show = a.online_id

        first = Background("first", lambda: session.probe_hosts([a, b]))
        self.gate.wait_entered()
        second = Background("second", lambda: session.probe_hosts([a, b, c]))
        assert not second.done.wait(QUIET)
        self.gate.release.set()
        first.join()
        second.join()

        assert first.error is None and second.error is None
        assert [online_id_of(url) for url in traffic.by("second")] == [c.online_id, c.online_id]
        assert set(session.known_sources) == {a.online_id, b.online_id, c.online_id}
        assert traffic.max_active == 1

    def test_each_operation_keeps_its_own_token_refresh_budget(self, opened, transport, episode_page, extract_seam):
        _engine, session = opened()
        a, b = session.sources[:2]
        traffic, gate = Traffic(), Gate()
        loads: dict[str, int] = {}
        refreshes = []

        def load(request):
            online_id = online_id_of(request.url)
            loads[online_id] = loads.get(online_id, 0) + 1
            return invalid_token() if loads[online_id] == 1 else "0"

        def page(_request):
            refreshes.append(1)
            if len(refreshes) == 1:
                gate.block()
            return episode_page()

        transport.route(EPISODE_URL, traffic.wrap(page))
        transport.route(is_load, traffic.wrap(load))
        transport.route(is_show, traffic.wrap(iframe))

        first = Background("first", lambda: session.resolve(a))
        gate.wait_entered()
        second = Background("second", lambda: session.resolve(b))
        assert not second.done.wait(QUIET)
        assert traffic.by("second") == []
        gate.release.set()
        first.join()
        second.join()

        assert first.error is None and second.error is None
        assert (first.result.source, second.result.source) == (a, b)
        assert len(refreshes) == 2
        assert loads == {a.online_id: 2, b.online_id: 2}
        assert set(session.known_sources) == {a.online_id, b.online_id}
        assert traffic.max_active == 1


class _CancelOnFailure(RecordingSink):
    """Cancels once a source failure was reported, i.e. between two WALK candidates."""

    def __init__(self, cancel: RecordingCancel) -> None:
        super().__init__()
        self._cancel = cancel

    def emit(self, event) -> None:
        super().emit(event)
        if isinstance(event, SourceAttemptFailed):
            self._cancel.cancel()


@pytest.mark.unit
class TestCancellation:
    def test_token_cancelled_before_the_call_makes_no_request(self, opened, transport, extract_seam, diag):
        _engine, session = opened()
        cancel = RecordingCancel()
        cancel.cancel()
        before = len(transport.requests)

        with pytest.raises(OperationCancelled):
            session.resolve(session.sources[0], candidates=session.sources, policy=WALK, cancel=cancel)
        with pytest.raises(OperationCancelled):
            session.probe_hosts(session.sources, cancel=cancel)

        assert len(transport.requests) == before
        diag.resolve_result.assert_not_called()

    def test_cancel_during_the_antibot_wait(self, opened, transport, extract_seam, diag, sink):
        engine, session = opened()
        a = session.sources[0]
        cancel = RecordingCancel(cancel_on_wait=1)

        with pytest.raises(OperationCancelled):
            session.resolve(a, candidates=session.sources, policy=WALK, cancel=cancel)

        assert xhr_calls(transport) == [(a.online_id, "player_load")]
        assert len(cancel.waits) == 1
        assert session.known_sources == {}
        extract_seam.assert_not_called()
        diag.resolve_result.assert_not_called()
        diag.host_health.assert_not_called()
        assert engine.health.state(embed_host(a.online_id)) is HostState.UNKNOWN
        assert sink.of(SourceAttemptFailed) == []
        assert (len(sink.of(PhaseStarted)), len(sink.of(PhaseFinished))) == (1, 1)

    @pytest.mark.parametrize("raised", ["no_stream_after_cancel", "operation_cancelled"])
    def test_cancel_during_extraction(self, opened, transport, extract_seam, diag, sink, raised):
        engine, session = opened()
        a = session.sources[0]
        cancel = RecordingCancel()

        def _extract(*_args, **_kwargs):
            if raised == "operation_cancelled":
                raise OperationCancelled()
            cancel.cancel()
            raise _hard_no_stream()

        extract_seam.side_effect = _extract

        with pytest.raises(OperationCancelled):
            session.resolve(a, candidates=session.sources, policy=WALK, cancel=cancel)

        extract_seam.assert_called_once()
        assert xhr_calls(transport) == [(a.online_id, "player_load"), (a.online_id, "player_show")]
        diag.resolve_result.assert_not_called()
        diag.host_health.assert_not_called()
        assert engine.health.state(embed_host(a.online_id)) is HostState.UNKNOWN
        assert sink.of(SourceAttemptFailed) == []
        # the embed resolved before the interrupted extraction; extraction itself added nothing
        assert set(session.known_sources) == {a.online_id}

    def test_cancel_during_extraction_on_a_cached_embed_runs_no_fresh_resolve(self, opened, transport, extract_seam, diag):
        engine, session = opened()
        a = session.sources[0]
        assert session.probe_hosts([a]) is None
        before = xhr_calls(transport)
        cancel = RecordingCancel()

        def _extract(*_args, **_kwargs):
            cancel.cancel()
            raise _hard_no_stream()

        extract_seam.side_effect = _extract

        with pytest.raises(OperationCancelled):
            session.resolve(a, cancel=cancel)

        assert xhr_calls(transport) == before
        extract_seam.assert_called_once()
        diag.resolve_result.assert_not_called()
        assert engine.health.state(embed_host(a.online_id)) is HostState.UNKNOWN
        assert session.known_sources[a.online_id].embed_url == embed_url(a.online_id)

    def test_cancel_between_walk_candidates(self, make_engine, transport, opened, extract_seam, diag):
        cancel = RecordingCancel()
        sink = _CancelOnFailure(cancel)
        engine, session = opened(events=sink)
        a, b = session.sources[:2]
        extract_seam.side_effect = _hard_no_stream()

        with pytest.raises(OperationCancelled):
            session.resolve(a, candidates=session.sources, policy=WALK, cancel=cancel)

        # the first candidate failed for real and stays recorded; the second was never started
        assert xhr_calls(transport) == [(a.online_id, "player_load"), (a.online_id, "player_show")]
        extract_seam.assert_called_once()
        assert [c.args[:2] for c in diag.resolve_result.call_args_list] == [(embed_host(a.online_id), False)]
        assert [c.args[0] for c in diag.player_selected.call_args_list] == [a.online_id]
        assert engine.health.state(embed_host(a.online_id)) is HostState.UNAVAILABLE
        assert engine.health.state(embed_host(b.online_id)) is HostState.UNKNOWN
        assert set(session.known_sources) == {a.online_id}

    def test_cancel_in_the_last_resort_pass(self, opened, transport, extract_seam, diag):
        engine, session = opened()
        a = session.sources[0]
        engine.health.record(embed_host(a.online_id), "seed", Signal.HARD)
        cancel = RecordingCancel()

        def _extract(*_args, **_kwargs):
            cancel.cancel()
            raise _hard_no_stream()

        extract_seam.side_effect = _extract

        with pytest.raises(OperationCancelled):
            session.resolve(a, candidates=[a], policy=WALK, cancel=cancel)

        extract_seam.assert_called_once()
        diag.resolve_result.assert_not_called()
        diag.host_health.assert_not_called()

    def test_cancel_from_another_thread_while_blocked_in_a_request(self, opened, transport, extract_seam, diag):
        engine, session = opened()
        a = session.sources[0]
        gate = Gate()
        cancel = RecordingCancel()
        transport.route(is_load, lambda _request: (gate.block(), "0")[1])

        op = Background("op", lambda: session.resolve(a, candidates=session.sources, policy=WALK, cancel=cancel))
        gate.wait_entered()
        cancel.cancel()
        gate.release.set()
        op.join()

        assert isinstance(op.error, OperationCancelled)
        assert xhr_calls(transport) == [(a.online_id, "player_load")]
        assert cancel.waits == []
        assert session.known_sources == {}
        extract_seam.assert_not_called()
        diag.resolve_result.assert_not_called()
        assert engine.health.state(embed_host(a.online_id)) is HostState.UNKNOWN

    def test_cancelled_probe_keeps_what_it_learned_and_requests_nothing_further(self, opened, transport):
        _engine, session = opened()
        a, b, _c = session.sources[:3]
        cancel = RecordingCancel(cancel_on_wait=2)

        with pytest.raises(OperationCancelled):
            session.probe_hosts(session.sources[:3], cancel=cancel)

        assert xhr_calls(transport) == [
            (a.online_id, "player_load"),
            (a.online_id, "player_show"),
            (b.online_id, "player_load"),
        ]
        assert set(session.known_sources) == {a.online_id}

    def test_session_stays_usable_after_a_cancelled_operation(self, opened, extract_seam):
        _engine, session = opened()
        a = session.sources[0]
        with pytest.raises(OperationCancelled):
            session.resolve(a, cancel=RecordingCancel(cancel_on_wait=1))

        assert session.resolve(a, cancel=RecordingCancel()).source is a


@pytest.mark.unit
class TestForceFresh:
    def test_drops_the_cached_embed_and_runs_the_full_xhr_path_again(self, opened, transport, extract_seam, sink):
        _engine, session = opened()
        a = session.sources[0]
        pair = [(a.online_id, "player_load"), (a.online_id, "player_show")]
        session.resolve(a)
        session.resolve(a)
        assert xhr_calls(transport) == pair
        sink.events.clear()
        cancel = RecordingCancel()

        resolved = session.resolve(a, force_fresh=True, cancel=cancel)

        assert xhr_calls(transport) == pair * 2
        assert len(cancel.waits) == 1
        assert sink.of(PhaseStarted) == [PhaseStarted(phase="antibot_delay")]
        assert resolved.embed.url == embed_url(a.online_id)
        assert set(session.known_sources) == {a.online_id}

    def test_without_force_fresh_the_cache_is_used_however_often(self, opened, transport, extract_seam):
        _engine, session = opened()
        a = session.sources[0]

        for _ in range(MAX_FRESH_RESOLVES + 3):
            session.resolve(a)

        assert len(xhr_calls(transport)) == 2

    def test_bounded_per_source_per_session(self, opened, transport, extract_seam):
        _engine, session = opened()
        a, b = session.sources[:2]
        session.resolve(a)
        for _ in range(MAX_FRESH_RESOLVES):
            session.resolve(a, force_fresh=True)
        before = len(transport.requests)

        with pytest.raises(FreshResolveLimitError) as exc_info:
            session.resolve(a, force_fresh=True)

        assert exc_info.value.code == "episode.fresh_resolve_limit"
        assert exc_info.value.params == {"limit": MAX_FRESH_RESOLVES}
        assert len(transport.requests) == before
        extract_calls = extract_seam.call_count
        assert set(session.known_sources) == {a.online_id}
        # the budget is per source, and the exhausted source still resolves from its cache
        assert session.resolve(b, force_fresh=True).source is b
        assert session.resolve(a).source is a
        assert extract_seam.call_count == extract_calls + 2

    def test_other_sources_cache_entries_are_untouched(self, opened, transport, extract_seam):
        _engine, session = opened()
        a, b = session.sources[:2]
        assert session.probe_hosts([a, b]) is None
        known_b = session.known_sources[b.online_id]

        session.resolve(a, force_fresh=True)

        assert session.known_sources[b.online_id] == known_b
        session.resolve(b)
        assert [online_id for online_id, _stage in xhr_calls(transport)].count(b.online_id) == 2
        assert [online_id for online_id, _stage in xhr_calls(transport)].count(a.online_id) == 4

    def test_force_fresh_applies_to_the_chosen_source_only_in_a_walk(self, opened, transport, extract_seam):
        _engine, session = opened()
        a, b = session.sources[:2]
        assert session.probe_hosts([a, b]) is None
        extract_seam.side_effect = lambda url, *_a, **_k: (_ for _ in ()).throw(NoStreamError("dead"))

        with pytest.raises(SourcesExhaustedError):
            session.resolve(a, candidates=[a, b], policy=WALK, force_fresh=True)

        calls = [online_id for online_id, stage in xhr_calls(transport) if stage == "player_load"]
        # a: probe + forced; b: probe + the one fresh resolve its stale cached embed earns
        assert calls.count(a.online_id) == 2
        assert calls.count(b.online_id) == 2

    def test_new_session_of_the_same_episode_resets_the_budget(self, opened, extract_seam):
        engine, session = opened()
        a = session.sources[0]
        for _ in range(MAX_FRESH_RESOLVES):
            session.resolve(a, force_fresh=True)
        with pytest.raises(FreshResolveLimitError):
            session.resolve(a, force_fresh=True)

        again = engine.open_episode(EPISODE)

        assert again is not session
        assert again.known_sources == {}
        for _ in range(MAX_FRESH_RESOLVES):
            assert again.resolve(a, force_fresh=True).source is a
        with pytest.raises(FreshResolveLimitError):
            again.resolve(a, force_fresh=True)


@pytest.mark.unit
class TestReload:
    @pytest.fixture
    def pages(self, transport, episode_page):
        """First the opened page, then one whose first player became the honeypot and whose token changed."""
        first = episode_page(TEST_TOKEN)
        changed = episode_page(OTHER_TOKEN).replace(TRAP_ID, "1825161")
        served = [first, changed]

        def page(_request):
            return served.pop(0) if len(served) > 1 else served[0]

        return page

    def test_rereads_the_page_in_place_once(self, opened, transport, pages, extract_seam):
        _engine, session = opened(page=pages)
        old_sources = session.sources
        a, b = old_sources[:2]
        assert a.online_id == "1825161"
        assert session.probe_hosts([a, b]) is None
        assert session.reload_used is False

        session.reload()

        assert session.reload_used is True
        assert page_requests(transport) == 2
        assert _ids(session.sources) == _ids(old_sources)[1:]
        assert session.known_sources == {}
        before = len(xhr_urls(transport))
        session.resolve(b)
        fresh = xhr_urls(transport)[before:]
        assert len(fresh) == 2
        assert all(quoted(OTHER_TOKEN) in url and quoted(TEST_TOKEN) not in url for url in fresh)

    def test_second_reload_is_refused_without_a_request(self, opened, transport, pages):
        _engine, session = opened(page=pages)
        session.reload()
        sources = session.sources
        before = len(transport.requests)

        with pytest.raises(ReloadLimitError) as exc_info:
            session.reload()

        assert exc_info.value.code == "episode.reload_limit"
        assert exc_info.value.params == {"number": 1.0}
        assert len(transport.requests) == before
        assert session.sources == sources

    def test_new_session_of_the_same_url_can_reload_again(self, opened, transport, episode_page):
        engine, session = opened()
        session.reload()
        with pytest.raises(ReloadLimitError):
            session.reload()

        again = engine.open_episode(EPISODE)

        assert again is not session
        assert again.reload_used is False
        again.reload()
        assert again.reload_used is True
        assert page_requests(transport) == 4
        with pytest.raises(ReloadLimitError):
            again.reload()

    def test_token_refresh_budget_is_one_per_operation_and_fresh_in_a_new_session(self, opened, transport, extract_seam):
        engine, session = opened()
        a = session.sources[0]
        transport.route(is_load, lambda _request: invalid_token())

        for _ in range(2):
            before = page_requests(transport), len(xhr_urls(transport))
            with pytest.raises(ShindenXhrError) as exc_info:
                session.resolve(a, candidates=session.sources, policy=WALK)
            assert xhr_outcome(exc_info.value) is XhrOutcome.ACCESS_BLOCKED
            # one refresh of the page, one retry of player_load, then the stop
            assert (page_requests(transport) - before[0], len(xhr_urls(transport)) - before[1]) == (1, 2)

        again = engine.open_episode(EPISODE)
        loads = []
        transport.route(is_load, lambda _request: (loads.append(1), invalid_token() if len(loads) == 1 else "0")[1])
        pages_before = page_requests(transport)

        assert again.resolve(a).source is a
        assert page_requests(transport) - pages_before == 1
        assert len(loads) == 2


@pytest.mark.unit
class TestProbeHosts:
    def test_sequential_and_stops_at_the_first_shared_api_error(self, opened, transport):
        _engine, session = opened()
        sources = session.sources
        one, two, three, four, five = _ids(sources[:5])
        replies = {two: FakeResponse(status_code=404), four: FakeResponse(status_code=503)}
        transport.route(is_load, lambda request: replies.get(online_id_of(request.url), "0"))

        stop = session.probe_hosts(sources)

        assert isinstance(stop, ShindenXhrError)
        assert xhr_outcome(stop) is XhrOutcome.SERVICE_UNAVAILABLE
        assert (stop.stage, stop.http_status) == ("player_load", 503)
        assert xhr_calls(transport) == [
            (one, "player_load"),
            (one, "player_show"),
            (two, "player_load"),
            (three, "player_load"),
            (three, "player_show"),
            (four, "player_load"),
        ]
        assert five not in "".join(transport.urls)
        assert session.known_sources == {
            one: PlayerSource(online_id=one, host=embed_host(one), embed_url=embed_url(one)),
            three: PlayerSource(online_id=three, host=embed_host(three), embed_url=embed_url(three)),
        }

    def test_known_sources_are_not_requested_again(self, opened, transport):
        _engine, session = opened()
        sources = session.sources
        one, two, three = _ids(sources[:3])
        transport.route(is_load, lambda request: FakeResponse(status_code=404) if online_id_of(request.url) == two else "0")
        assert session.probe_hosts(sources[:3]) is None
        assert set(session.known_sources) == {one, three}
        before = len(xhr_calls(transport))

        assert session.probe_hosts(sources[:3]) is None

        assert xhr_calls(transport)[before:] == [(two, "player_load")]
        assert session.probe_hosts([sources[0], sources[2]]) is None
        assert len(xhr_calls(transport)) == before + 1

    def test_sources_learned_by_resolve_count_as_known(self, opened, transport, extract_seam):
        _engine, session = opened()
        a = session.sources[0]
        extract_seam.side_effect = NoStreamError("dead")
        with pytest.raises(SourcesExhaustedError):
            session.resolve(a)
        before = len(transport.requests)

        assert session.probe_hosts([a]) is None

        assert len(transport.requests) == before
        assert session.known_sources[a.online_id].host == embed_host(a.online_id)

    def test_no_iframe_and_non_xhr_source_errors_are_skipped(self, opened, transport, monkeypatch):
        _engine, session = opened()
        a, b, c = session.sources[:3]
        transport.route(is_show, lambda request: "" if online_id_of(request.url) == a.online_id else iframe(request))

        assert session.probe_hosts([a, b]) is None
        assert set(session.known_sources) == {b.online_id}

        from alt_ani_core.services import episode_session

        monkeypatch.setattr(
            episode_session.shinden_api, "resolve_embed", lambda *_a, **_k: (_ for _ in ()).throw(AntiBotError("blocked"))
        )
        assert session.probe_hosts([c]) is None
        assert set(session.known_sources) == {b.online_id}

    def test_probe_records_no_health_and_no_resolve_result(self, opened, transport, diag):
        engine, session = opened()
        a, b = session.sources[:2]
        transport.route(
            is_load, lambda request: FakeResponse(status_code=404) if online_id_of(request.url) == a.online_id else "0"
        )

        assert session.probe_hosts([a, b]) is None

        diag.resolve_result.assert_not_called()
        diag.host_health.assert_not_called()
        assert engine.health.state(embed_host(b.online_id)) is HostState.UNKNOWN

    def test_display_host_drops_the_www_prefix(self, opened, transport):
        _engine, session = opened()
        a = session.sources[0]
        transport.route(is_show, '<iframe src="//www.host.example/e/1?t=1"></iframe>')

        assert session.probe_hosts([a]) is None

        assert session.known_sources[a.online_id] == PlayerSource(
            online_id=a.online_id, host="host.example", embed_url="https://www.host.example/e/1?t=1"
        )

    def test_honeypot_is_never_listed_or_requested(self, opened, transport, extract_seam):
        _engine, session = opened()
        assert TRAP_ID not in _ids(session.sources)

        assert session.probe_hosts(session.sources) is None
        assert len(session.known_sources) == len(session.sources)
        assert not any(TRAP_ID in url for url in transport.urls)

        trap = PlayerEntry(online_id=TRAP_ID, player="Trap", lang_audio="jp", lang_subs="pl")
        before = len(transport.requests)
        with pytest.raises(AssertionError):
            session.probe_hosts([trap])
        with pytest.raises(AssertionError):
            session.resolve(trap)
        assert len(transport.requests) == before
        assert TRAP_ID not in session.known_sources


@pytest.mark.unit
class TestSelect:
    @pytest.fixture
    def mixed_page(self, episode_page):
        """The fixture page with its last player (1597539) switched to Polish audio."""
        html = episode_page()
        at = html.index('"audio": "jp"', html.index('{"online_id": 1597539'))
        return html[:at] + '"audio": "pl"' + html[at + len('"audio": "jp"') :]

    def test_unmatched_filter_leaves_candidates_empty_and_the_pool_whole(self, opened):
        _engine, session = opened()

        selection = session.select(SourceFilter(lang="de"), purpose=Purpose.WATCH)

        assert selection.candidates == ()
        assert selection.unmatched == ("--lang=de",)
        assert len(selection.all_sources) == 9
        assert set(selection.all_sources) == set(session.sources)

    def test_every_unmatched_criterion_is_reported(self, opened):
        _engine, session = opened()

        selection = session.select(SourceFilter(lang="de", subs="fr", player_name="Nope"), purpose=Purpose.WATCH)

        assert selection.candidates == ()
        assert selection.unmatched == ("--player-name=Nope", "--lang=de", "--subs=fr")

    def test_no_filter_selects_everything_in_ranked_order(self, opened):
        _engine, session = opened()

        selection = session.select(SourceFilter(), purpose=Purpose.WATCH)

        assert selection.unmatched == ()
        assert selection.candidates == selection.all_sources
        assert _ids(selection.candidates) == [
            "1825161",
            "1825160",
            "1879668",
            "1879635",
            "1863843",
            "1825159",
            "1882328",
            "1598004",
            "1597539",
        ]

    def test_matching_filter_narrows_candidates_only(self, opened):
        _engine, session = opened()

        selection = session.select(SourceFilter(subs="pl", player_name="dood"), purpose=Purpose.WATCH)

        assert _ids(selection.candidates) == ["1598004"]
        assert selection.unmatched == ()
        assert len(selection.all_sources) == 9

    def test_order_depends_on_the_purpose(self, opened, mixed_page):
        _engine, session = opened(page=mixed_page)

        watch = session.select(SourceFilter(), purpose=Purpose.WATCH)
        download = session.select(SourceFilter(), purpose=Purpose.DOWNLOAD)

        assert _ids(watch.all_sources)[0] == "1597539"
        assert _ids(download.all_sources)[-1] == "1597539"
        assert _ids(watch.all_sources)[1:] == _ids(download.all_sources)[:-1]
        assert _ids(watch.candidates) == _ids(watch.all_sources)
        assert _ids(download.candidates) == _ids(download.all_sources)

    def test_select_makes_no_request_and_ignores_health(self, opened, transport, diag):
        engine, session = opened()
        engine.health.learn_label("Mega", "mega.example")
        engine.health.record("mega.example", "seed", Signal.SUCCESS)
        before = len(transport.requests)

        selection = session.select(SourceFilter(), purpose=Purpose.WATCH)

        assert _ids(selection.candidates)[:2] == ["1825161", "1825160"]
        assert len(transport.requests) == before
        diag.health_reorder.assert_not_called()


@pytest.mark.unit
class TestRank:
    def test_without_health_evidence_the_order_is_kept_and_nothing_is_logged(self, opened, diag):
        _engine, session = opened()
        players = list(session.select(SourceFilter(), purpose=Purpose.WATCH).candidates)

        assert session.rank(players) == players
        diag.health_reorder.assert_not_called()

    def test_healthy_host_moves_up_inside_its_group_only(self, opened, diag_events):
        engine, session = opened()
        players = list(session.select(SourceFilter(), purpose=Purpose.WATCH).candidates)
        snapshot = list(players)
        engine.health.learn_label("Dood", "dood.example")
        engine.health.record("dood.example", "seed", Signal.SUCCESS)
        engine.health.learn_label("Gdrive", "gdrive.example")
        engine.health.record("gdrive.example", "seed", Signal.HARD)

        ranked = session.rank(players)

        # groups: six 1080p jp/pl, two 720p jp/pl (Gdrive, Dood), one 720p jp/en (Dood)
        assert _ids(ranked) == [
            "1825160",
            "1879668",
            "1879635",
            "1863843",
            "1825159",
            "1825161",
            "1598004",
            "1882328",
            "1597539",
        ]
        assert players == snapshot
        reorders = [fields for event, fields in diag_events if event == "health_reorder"]
        assert reorders == [
            {
                "number": 1.0,
                "before": ",".join(_ids(snapshot)),
                "after": ",".join(_ids(ranked)),
                "states": "unknown,unknown,unknown,unknown,unknown,unavailable,healthy,unavailable,healthy",
            }
        ]

    def test_rank_makes_no_request_and_does_not_touch_the_session(self, opened, transport):
        engine, session = opened()
        engine.health.learn_label("Mega", "mega.example")
        engine.health.record("mega.example", "seed", Signal.SUCCESS)
        sources = session.sources
        before = len(transport.requests)

        ranked = session.rank(list(sources))

        assert _ids(ranked)[0] == "1825160"
        assert sorted(_ids(ranked)) == sorted(_ids(sources))
        assert session.sources == sources
        assert len(transport.requests) == before
