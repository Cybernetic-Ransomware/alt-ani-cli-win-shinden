import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import FreshResolveLimitError, OperationCancelled, ReloadLimitError, StaleSessionError
from alt_ani_core.services import episode_session

from .support import EPISODE_URL, is_load, page_requests


@pytest.mark.unit
class TestInterruptedOperationsSpendNothing:
    def test_cancelled_reload_leaves_the_reload_available(self, opened, transport, episode_page):
        _engine, session = opened()
        token = CancelToken()

        def cancel_then_serve(_request):
            token.cancel()
            return episode_page()

        transport.route(EPISODE_URL, cancel_then_serve)
        with pytest.raises(OperationCancelled):
            session.reload(cancel=token)
        assert session.reload_used is False

        transport.route(EPISODE_URL, episode_page())
        session.reload()
        with pytest.raises(ReloadLimitError):
            session.reload()

    def test_failed_reload_still_counts(self, opened, transport):
        from alt_ani_core.testing import FakeResponse
        from alt_ani_core.transport.api import HttpStatusError

        _engine, session = opened()
        transport.route(EPISODE_URL, FakeResponse(status_code=500))
        with pytest.raises(HttpStatusError):
            session.reload()
        with pytest.raises(ReloadLimitError):
            session.reload()

    def test_cancelled_forced_resolve_is_not_counted(self, opened, transport, extract_seam):
        _engine, session = opened()
        chosen = session.sources[0]
        session.resolve(chosen)

        for _ in range(3):
            token = CancelToken()

            def cancel_on_load(_request, token=token):
                token.cancel()
                return "0"

            transport.route(is_load, cancel_on_load)
            with pytest.raises(OperationCancelled):
                session.resolve(chosen, force_fresh=True, cancel=token)

        transport.route(is_load, "0")
        for _ in range(episode_session.MAX_FRESH_RESOLVES):
            session.resolve(chosen, force_fresh=True)
        with pytest.raises(FreshResolveLimitError):
            session.resolve(chosen, force_fresh=True)


@pytest.mark.unit
class TestStaleSessionOpensNothing:
    def test_stale_session_is_refused_before_any_new_http_session(self, opened, transport, extract_seam):
        engine, session = opened()
        sessions_before = len(transport.sessions)
        pages_before = page_requests(transport)

        engine.set_access(AccessConfig(use_flaresolverr=False))

        with pytest.raises(StaleSessionError):
            session.resolve(session.sources[0])
        with pytest.raises(StaleSessionError):
            session.reload()
        assert len(transport.sessions) == sessions_before
        assert page_requests(transport) == pages_before
        assert session.reload_used is False
