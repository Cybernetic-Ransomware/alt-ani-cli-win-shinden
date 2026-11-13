import pytest

from alt_ani_core.config import SHINDEN_API_BASE, SHINDEN_XHR_BASE
from alt_ani_core.errors import ParseError, ShindenXhrError, SourcesExhaustedError
from alt_ani_core.events import SourceAttemptFailed, XhrFallback
from alt_ani_core.models import FallbackPolicy
from alt_ani_core.shinden.api import XhrOutcome, xhr_outcome
from alt_ani_core.testing import FakeResponse, RecordingCancel

from .support import (
    EPISODE,
    EPISODE_2,
    OTHER_TOKEN,
    TEST_TOKEN,
    TRAP_ID,
    invalid_token,
    is_load,
    online_id_of,
    page_requests,
    quoted,
    route_xhr,
    xhr_urls,
)

WALK = FallbackPolicy.WALK
_TOKENS = (TEST_TOKEN, OTHER_TOKEN, quoted(TEST_TOKEN), quoted(OTHER_TOKEN))


@pytest.fixture
def two_episodes(make_engine, transport, episode_page, extract_seam):
    """Engine with both episode pages routed; each page gets the token given for it (None: no token on the page)."""

    def _make(token_1: str | None = TEST_TOKEN, token_2: str | None = OTHER_TOKEN, **config):
        transport.route(EPISODE.url, episode_page(token_1))
        transport.route(EPISODE_2.url, episode_page(token_2))
        route_xhr(transport)
        return make_engine(**config)

    return _make


def _context_events(diag_events, action: str) -> list[dict]:
    return [fields for event, fields in diag_events if event == "shinden_xhr_context" and fields["action"] == action]


@pytest.mark.unit
class TestEpisodeIsolation:
    def test_each_session_uses_only_its_own_token(self, two_episodes, transport):
        engine = two_episodes()
        first = engine.open_episode(EPISODE)
        second = engine.open_episode(EPISODE_2)
        marks = [len(transport.urls)]

        first.resolve(first.sources[0])
        marks.append(len(transport.urls))
        second.resolve(second.sources[0])
        marks.append(len(transport.urls))
        first.resolve(first.sources[1])
        second.probe_hosts(second.sources[:3])

        urls = transport.urls
        own_1 = urls[marks[0] : marks[1]] + urls[marks[2] : marks[2] + 2]
        own_2 = urls[marks[1] : marks[2]] + urls[marks[2] + 2 :]
        enc1, enc2 = quoted(TEST_TOKEN), quoted(OTHER_TOKEN)
        assert len(own_1) == 4 and len(own_2) == 6
        assert all(enc1 in u and enc2 not in u for u in own_1)
        assert all(enc2 in u and enc1 not in u for u in own_2)
        assert all(u.startswith(SHINDEN_XHR_BASE) for u in xhr_urls(transport))

    def test_reopening_an_episode_reads_its_token_again(self, make_engine, transport, episode_page, extract_seam):
        pages = [episode_page(TEST_TOKEN), episode_page(OTHER_TOKEN)]
        transport.route(EPISODE.url, lambda _request: pages.pop(0))
        route_xhr(transport)
        engine = make_engine()

        old = engine.open_episode(EPISODE)
        new = engine.open_episode(EPISODE)
        new.resolve(new.sources[0])
        old.resolve(old.sources[0])

        new_urls, old_urls = xhr_urls(transport)[:2], xhr_urls(transport)[2:]
        assert all(quoted(OTHER_TOKEN) in u for u in new_urls)
        assert all(quoted(TEST_TOKEN) in u for u in old_urls)

    def test_fallback_on_one_episode_does_not_carry_over(self, two_episodes, transport, diag_events, sink):
        engine = two_episodes(token_1=None)

        first = engine.open_episode(EPISODE)
        first.resolve(first.sources[0])
        mark = len(xhr_urls(transport))
        second = engine.open_episode(EPISODE_2)
        second.resolve(second.sources[0])

        calls = xhr_urls(transport)
        assert len(calls) == 4
        assert all(u.startswith(SHINDEN_API_BASE) for u in calls[:mark])
        assert all(u.startswith(SHINDEN_XHR_BASE) for u in calls[mark:])
        assert _context_events(diag_events, "fallback") == [
            {"action": "fallback", "backend": "api4", "reason": "auth_missing", "online_id": None}
        ]
        assert sink.of(XhrFallback) == [XhrFallback(episode=1.0, reason="auth_missing")]
        results = [fields for event, fields in diag_events if event == "resolve_result"]
        assert [(f["xhr_backend"], f["xhr_fallback"]) for f in results] == [("api4", "auth_missing"), ("new_xhr", None)]

    def test_fallback_is_decided_once_per_episode_not_per_player(self, two_episodes, transport, diag_events, sink):
        engine = two_episodes(token_1=None, token_2=None)
        transport.route(is_load, FakeResponse(status_code=404))

        for episode in (EPISODE, EPISODE_2):
            session = engine.open_episode(episode)
            with pytest.raises(SourcesExhaustedError) as exc_info:
                session.resolve(session.sources[0], candidates=session.sources, policy=WALK)
            assert len(exc_info.value.attempts) == len(session.sources) == 9

        calls = xhr_urls(transport)
        assert len(calls) == 18
        assert all(u.startswith(SHINDEN_API_BASE) for u in calls)
        assert len(_context_events(diag_events, "fallback")) == 2
        assert sink.of(XhrFallback) == [
            XhrFallback(episode=1.0, reason="auth_missing"),
            XhrFallback(episode=2.0, reason="auth_missing"),
        ]

    @pytest.mark.parametrize(
        ("page_kwargs", "reason"),
        [
            ({"xhr": None}, "endpoint_missing"),
            ({"xhr": "//evil.example/xhr"}, "endpoint_rejected"),
            ({"token": "short"}, "auth_malformed"),
        ],
        ids=["endpoint-missing", "endpoint-rejected", "auth-malformed"],
    )
    def test_fallback_reason_reaches_the_event_and_diagnostics(
        self, make_engine, transport, episode_page, extract_seam, diag_events, sink, page_kwargs, reason
    ):
        transport.route(EPISODE.url, episode_page(**page_kwargs))
        route_xhr(transport)
        engine = make_engine()

        session = engine.open_episode(EPISODE)
        session.resolve(session.sources[0])

        assert sink.of(XhrFallback) == [XhrFallback(episode=1.0, reason=reason)]
        assert _context_events(diag_events, "fallback") == [
            {"action": "fallback", "backend": "api4", "reason": reason, "online_id": None}
        ]
        assert all(u.startswith(SHINDEN_API_BASE) for u in xhr_urls(transport))
        assert "evil.example" not in "".join(transport.urls)

    def test_token_page_reports_no_fallback(self, two_episodes, diag_events, sink):
        engine = two_episodes()

        engine.open_episode(EPISODE)

        assert sink.of(XhrFallback) == []
        assert [fields for event, fields in diag_events if event == "shinden_xhr_context"] == []


@pytest.mark.unit
class TestStops:
    @pytest.fixture
    def changed_list(self, make_engine, transport, episode_page, extract_seam):
        """The chosen player turns into the honeypot on the page read by the token refresh."""
        first = episode_page(TEST_TOKEN)
        refreshed = episode_page(OTHER_TOKEN).replace(TRAP_ID, "1825161")
        pages = [first, refreshed]
        loads = [invalid_token()]
        transport.route(EPISODE.url, lambda _request: pages.pop(0) if len(pages) > 1 else pages[0])
        route_xhr(transport, load=lambda _request: loads.pop(0) if loads else "0")
        return make_engine()

    @pytest.mark.parametrize("policy", [FallbackPolicy.SINGLE, WALK])
    def test_player_list_change_after_refresh_stops_the_operation(
        self, changed_list, transport, extract_seam, diag_events, sink, policy
    ):
        session = changed_list.open_episode(EPISODE)
        chosen = session.sources[0]
        assert chosen.online_id == "1825161"

        with pytest.raises(ShindenXhrError) as exc_info:
            session.resolve(chosen, candidates=session.sources, policy=policy)

        err = exc_info.value
        assert xhr_outcome(err) is XhrOutcome.PLAYERS_CHANGED
        assert (err.stage, err.reason) == ("token_refresh", "player_is_honeypot")
        assert len(xhr_urls(transport)) == 1
        assert page_requests(transport) == 2
        extract_seam.assert_not_called()
        assert sink.of(SourceAttemptFailed) == []
        results = [fields for event, fields in diag_events if event == "resolve_result"]
        assert len(results) == 1
        assert (results[0]["ok"], results[0]["xhr_outcome"], results[0]["token_refreshed"]) == (False, "players_changed", True)
        blob = repr(err) + str(err) + repr(sink.events) + repr(diag_events)
        assert not any(token in blob for token in _TOKENS)

    def test_invalid_backend_value_is_refused_before_any_request(self, make_engine, transport):
        with pytest.raises(ValueError, match="ALT_ANI_CLI_XHR_BACKEND.*legacy"):
            make_engine(xhr_backend="legacy")

        assert transport.sessions == [] and transport.requests == []


@pytest.mark.unit
class TestForcedApi4:
    def test_forced_api4_ignores_the_token_and_records_the_choice(self, two_episodes, transport, diag_events, sink):
        engine = two_episodes(xhr_backend="api4")

        for episode in (EPISODE, EPISODE_2):
            session = engine.open_episode(episode)
            session.resolve(session.sources[0])

        calls = xhr_urls(transport)
        assert len(calls) == 4
        assert all(u.startswith(SHINDEN_API_BASE) for u in calls)
        assert not any(token in u for u in calls for token in _TOKENS)
        contexts = [fields for event, fields in diag_events if event == "shinden_xhr_context"]
        assert [f["action"] for f in contexts] == ["forced", "forced"]
        assert {f["backend"] for f in contexts} == {"api4"}
        results = [fields for event, fields in diag_events if event == "resolve_result"]
        assert len(results) == 2
        assert {f["xhr_backend"] for f in results} == {"api4"}
        assert sink.of(XhrFallback) == []

    def test_forced_api4_never_refreshes_on_invalid_token(self, two_episodes, transport):
        engine = two_episodes(xhr_backend="api4")
        transport.route(is_load, lambda _request: invalid_token())
        session = engine.open_episode(EPISODE)

        with pytest.raises(ShindenXhrError) as exc_info:
            session.resolve(session.sources[0], candidates=session.sources, policy=WALK)

        assert xhr_outcome(exc_info.value) is XhrOutcome.ACCESS_BLOCKED
        assert len(xhr_urls(transport)) == 1
        assert page_requests(transport) == 1


@pytest.mark.unit
class TestEpisodePageDrift:
    @pytest.mark.parametrize("backend", ["new", "api4"])
    def test_page_without_honeypot_declaration_stops_before_any_xhr(
        self, make_engine, transport, episode_page, diag_events, sink, backend
    ):
        page = episode_page().replace(f'"pulapka": {TRAP_ID}', '"renamed": 1', 1)
        transport.route(EPISODE.url, page)
        route_xhr(transport)
        engine = make_engine(xhr_backend=backend)

        with pytest.raises(ParseError, match="honeypot"):
            engine.open_episode(EPISODE)

        assert xhr_urls(transport) == []
        assert sink.of(XhrFallback) == []
        assert [fields for event, fields in diag_events if event == "shinden_xhr_context"] == []


@pytest.mark.unit
class TestTokenSecrecy:
    def test_token_never_reaches_events_diagnostics_or_repr(self, two_episodes, transport, extract_seam, diag_events, sink):
        engine = two_episodes()
        loads: dict[str, int] = {}

        def load(request):
            online_id = online_id_of(request.url)
            loads[online_id] = loads.get(online_id, 0) + 1
            if online_id == "1825161":
                return invalid_token() if loads[online_id] == 1 else FakeResponse(status_code=404)
            return "0"

        transport.route(is_load, load)
        session = engine.open_episode(EPISODE)
        other = engine.open_episode(EPISODE_2)
        errors: list[BaseException] = []

        resolved = session.resolve(session.sources[0], candidates=session.sources, policy=WALK, cancel=RecordingCancel())
        other.probe_hosts(other.sources[:2])
        transport.route(is_load, FakeResponse(status_code=429, headers={"Retry-After": "30"}))
        for call in (lambda: session.resolve(session.sources[2]), lambda: other.resolve(other.sources[3])):
            with pytest.raises(ShindenXhrError) as exc_info:
                call()
            errors.append(exc_info.value)

        assert resolved.source is session.sources[1]
        assert len(sink.of(SourceAttemptFailed)) == 1
        assert any(event == "shinden_xhr_context" and fields["action"] == "token_refresh" for event, fields in diag_events)
        assert any(quoted(TEST_TOKEN) in url for url in transport.urls)
        blob = "".join(
            [
                repr(sink.events),
                repr(diag_events),
                repr(vars(session)),
                repr(vars(other)),
                repr(resolved),
                repr(session.known_sources),
                *(repr(err) + str(err) + repr(vars(err)) for err in errors),
            ]
        )
        assert not any(token in blob for token in _TOKENS)

    def test_token_never_reaches_the_diagnostics_log_file(self, two_episodes, transport, extract_seam, tmp_path):
        from alt_ani_core import diagnostics

        log = diagnostics.configure(tmp_path / "diag")
        engine = two_episodes()
        loads = [invalid_token()]
        transport.route(is_load, lambda _request: loads.pop(0) if loads else "0")
        session = engine.open_episode(EPISODE)

        session.resolve(session.sources[0])

        text = log.read_text(encoding="utf-8")
        assert "event=resolve_result" in text and "token_refreshed=True" in text
        assert not any(token in text for token in _TOKENS)
