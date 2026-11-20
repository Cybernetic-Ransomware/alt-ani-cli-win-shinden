from unittest.mock import MagicMock, patch

import pytest

from alt_ani_core import extract
from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import NoStreamError, OperationCancelled, SourcesExhaustedError, StaleSessionError
from alt_ani_core.events import ExtractorFallback
from alt_ani_core.models import FallbackPolicy
from alt_ani_core.rules.health import HostState
from alt_ani_core.services.engine import GuardedSession
from alt_ani_core.testing import FakeRequest, make_stream
from alt_ani_core.transport.api import NetworkError

from .support import EPISODE, EPISODE_URL, Background, Gate, embed_host, iframe, is_show, route_xhr

_VIDARA = "vidara.to"


def _retire(engine, how: str) -> None:
    if how == "close":
        engine.close()
    else:
        engine.set_access(AccessConfig(use_flaresolverr=False))


def _is_embed_request(request: FakeRequest) -> bool:
    return "/xhr/" not in request.url and "shinden.pl" not in request.url


def _failing_after(gate: Gate):
    def reply(_request: FakeRequest):
        gate.block()
        return NetworkError("connection reset")

    return reply


def _vidara_iframe(request: FakeRequest) -> str:
    online_id = request.url.split("/xhr/", 1)[1].split("/", 1)[0]
    return f'<iframe src="https://{_VIDARA}/e/{online_id}"></iframe>'


@pytest.fixture
def ytdlp():
    with patch("alt_ani_core.extract.ytdlp_resolver.resolve", return_value=make_stream()) as mock:
        yield mock


@pytest.mark.unit
class TestGuardedSessionErrorPath:
    @pytest.mark.parametrize("method", ["get", "post"])
    def test_failure_after_invalidation_is_reported_as_the_interrupt(self, method):
        stale = False

        def check() -> None:
            if stale:
                raise StaleSessionError()

        def request(*_args, **_kwargs):
            nonlocal stale
            stale = True
            raise NetworkError("connection reset")

        inner = MagicMock(**{f"{method}.side_effect": request})
        with pytest.raises(StaleSessionError) as info:
            getattr(GuardedSession(inner, check), method)("https://h.example/api")
        assert info.value.__context__ is None
        assert info.value.__cause__ is None

    @pytest.mark.parametrize("method", ["get", "post"])
    def test_failure_on_a_live_session_stays_the_transport_error(self, method):
        inner = MagicMock(**{f"{method}.side_effect": NetworkError("connection reset")})
        with pytest.raises(NetworkError):
            getattr(GuardedSession(inner, lambda: None), method)("https://h.example/api")

    @pytest.mark.parametrize("method", ["get", "post"])
    def test_cancel_during_a_failing_request_is_reported_as_cancel(self, method):
        token = CancelToken()

        def request(*_args, **_kwargs):
            token.cancel()
            raise NetworkError("connection reset")

        inner = MagicMock(**{f"{method}.side_effect": request})
        with pytest.raises(OperationCancelled):
            getattr(GuardedSession(inner, token.raise_if_cancelled), method)("https://h.example/api")


@pytest.mark.unit
class TestDispatcherChecksLivenessBeforeFallback:
    def test_stale_operation_starts_no_fallback_after_a_plain_extractor_error(self, ytdlp):
        stale = False

        def check() -> None:
            if stale:
                raise StaleSessionError()

        def failing_resolver(*_args):
            nonlocal stale
            stale = True
            raise ValueError("parse failure")

        on_fallback = MagicMock()
        rule = extract.HostRule("custom", failing_resolver)
        with patch.dict(extract.HOST_RULES, {"stale.example": rule}), pytest.raises(StaleSessionError):
            extract.resolve(
                "https://stale.example/e/1", "https://shinden.pl/", transport=MagicMock(), on_fallback=on_fallback, check=check
            )
        ytdlp.assert_not_called()
        on_fallback.assert_not_called()

    def test_stale_operation_starts_no_generic_fallback(self, ytdlp):
        on_fallback = MagicMock()

        def check() -> None:
            if on_fallback.mock_calls or jw.called:
                raise StaleSessionError()

        with (
            patch("alt_ani_core.extract.jwplayer.resolve", side_effect=ValueError("no player")) as jw,
            pytest.raises(StaleSessionError),
        ):
            extract.resolve("https://unknown.example/e/1", "r", transport=MagicMock(), on_fallback=on_fallback, check=check)
        ytdlp.assert_not_called()
        on_fallback.assert_not_called()

    @pytest.mark.parametrize("path", ["custom", "generic"])
    def test_fallback_callback_cancelling_the_token_starts_no_ytdlp(self, ytdlp, path):
        token = CancelToken()
        on_fallback = MagicMock(side_effect=lambda *_a: token.cancel())
        url = "https://custom.example/e/1" if path == "custom" else "https://unknown.example/e/1"
        rule = extract.HostRule("custom", MagicMock(side_effect=ValueError("parse failure")))
        with (
            patch.dict(extract.HOST_RULES, {"custom.example": rule}),
            patch("alt_ani_core.extract.jwplayer.resolve", side_effect=ValueError("no player")),
            pytest.raises(OperationCancelled),
        ):
            extract.resolve(url, "r", transport=MagicMock(), on_fallback=on_fallback, cancel=token, check=lambda: None)
        on_fallback.assert_called_once()
        ytdlp.assert_not_called()

    @pytest.mark.parametrize("path", ["custom", "generic"])
    def test_fallback_callback_invalidating_the_generation_starts_no_ytdlp(self, ytdlp, path):
        stale = False

        def check() -> None:
            if stale:
                raise StaleSessionError()

        def retire(*_args) -> None:
            nonlocal stale
            stale = True

        on_fallback = MagicMock(side_effect=retire)
        url = "https://custom.example/e/1" if path == "custom" else "https://unknown.example/e/1"
        rule = extract.HostRule("custom", MagicMock(side_effect=ValueError("parse failure")))
        with (
            patch.dict(extract.HOST_RULES, {"custom.example": rule}),
            patch("alt_ani_core.extract.jwplayer.resolve", side_effect=ValueError("no player")),
            pytest.raises(StaleSessionError),
        ):
            extract.resolve(url, "r", transport=MagicMock(), on_fallback=on_fallback, check=check)
        on_fallback.assert_called_once()
        ytdlp.assert_not_called()

    def test_live_operation_still_falls_back(self, ytdlp):
        on_fallback = MagicMock()
        with patch("alt_ani_core.extract.jwplayer.resolve", side_effect=ValueError("no player")):
            extract.resolve(
                "https://unknown.example/e/1", "r", transport=MagicMock(), on_fallback=on_fallback, check=lambda: None
            )
        ytdlp.assert_called_once()
        on_fallback.assert_called_once()


@pytest.mark.unit
class TestInvalidatedDuringFailingExtractorRequest:
    """set_access()/close() while an extractor request is in flight and then fails: no fallback, nothing recorded."""

    @pytest.mark.parametrize("how", ["set_access", "close"])
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_no_fallback_no_penalty_no_success(self, opened, transport, diag, sink, ytdlp, how, method):
        engine, session = opened()
        a = session.sources[0]
        if method == "POST":
            transport.route(is_show, _vidara_iframe)
            host = _VIDARA
        else:
            host = embed_host(a.online_id)
        gate = Gate()
        transport.route(lambda r: _is_embed_request(r) and r.method == method, _failing_after(gate))

        op = Background("op", lambda: session.resolve(a, candidates=session.sources, policy=FallbackPolicy.WALK))
        gate.wait_entered()
        _retire(engine, how)
        gate.release.set()
        op.join()

        assert isinstance(op.error, StaleSessionError)
        assert op.error.__context__ is None
        ytdlp.assert_not_called()
        assert sink.of(ExtractorFallback) == []
        diag.resolve_result.assert_not_called()
        diag.host_health.assert_not_called()
        assert engine.health.state(host) is HostState.UNKNOWN
        assert [r.method for r in transport.requests if _is_embed_request(r)] == [method]

    def test_cancel_during_failing_request_starts_no_fallback(self, opened, transport, diag, sink, ytdlp):
        _engine, session = opened()
        a = session.sources[0]
        gate = Gate()
        transport.route(_is_embed_request, _failing_after(gate))
        token = CancelToken()

        op = Background("op", lambda: session.resolve(a, cancel=token))
        gate.wait_entered()
        token.cancel()
        gate.release.set()
        op.join()

        assert isinstance(op.error, OperationCancelled)
        ytdlp.assert_not_called()
        assert sink.of(ExtractorFallback) == []
        diag.resolve_result.assert_not_called()

    @pytest.mark.parametrize("how", ["cancel", "set_access", "close"])
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_fallback_event_listener_interrupting_the_operation_starts_no_ytdlp(
        self, make_engine, transport, episode_page, diag, ytdlp, how, method
    ):
        token = CancelToken()
        engines: list = []

        class _InterruptingSink:
            def __init__(self) -> None:
                self.fallbacks = 0

            def emit(self, event) -> None:
                if isinstance(event, ExtractorFallback):
                    self.fallbacks += 1
                    token.cancel() if how == "cancel" else _retire(engines[0], how)

        sink = _InterruptingSink()
        transport.route(EPISODE_URL, episode_page())
        route_xhr(transport, show=_vidara_iframe if method == "POST" else iframe)
        transport.route(_is_embed_request, NetworkError("connection reset"))
        engines.append(make_engine(events=sink))
        session = engines[0].open_episode(EPISODE)

        with pytest.raises(OperationCancelled if how == "cancel" else StaleSessionError):
            session.resolve(session.sources[0], cancel=token)

        assert sink.fallbacks == 1
        ytdlp.assert_not_called()
        diag.resolve_result.assert_not_called()

    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_failure_on_a_live_session_still_falls_back_to_ytdlp(self, opened, transport, sink, ytdlp, method):
        _engine, session = opened()
        a = session.sources[0]
        if method == "POST":
            transport.route(is_show, _vidara_iframe)
        transport.route(_is_embed_request, NetworkError("connection reset"))

        resolved = session.resolve(a)

        assert resolved.source == a
        ytdlp.assert_called_once()
        assert [event.kind for event in sink.of(ExtractorFallback)] == [
            "jwplayer_fallback" if method == "GET" else "extractor_fallback"
        ]

    def test_failure_on_a_live_session_with_failing_fallback_is_the_sources_failure(self, opened, transport, diag, ytdlp):
        _engine, session = opened()
        a = session.sources[0]
        transport.route(_is_embed_request, NetworkError("connection reset"))
        ytdlp.side_effect = NoStreamError("ytdlp failed")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(a)

        ytdlp.assert_called_once()
        diag.resolve_result.assert_called_once()
