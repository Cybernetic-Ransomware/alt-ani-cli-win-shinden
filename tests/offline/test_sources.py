import pytest

from alt_ani_core.cancel import NEVER
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import AccessBlockedError, NoStreamError, ShindenXhrError, SourcesExhaustedError
from alt_ani_core.models import ResolvedStream, SourceAttempt
from alt_ani_core.services.offline.interaction import SCOPE_HOST, SCOPE_SHINDEN, InteractionNeeded
from alt_ani_core.services.offline.sources import EngineSources, SourceUnavailable, _SessionSources
from alt_ani_core.shinden import api as shinden_api

from .support import episode, player, progressive


class _Session:
    def __init__(self, error: BaseException | None = None) -> None:
        self.sources = (player("1", audio="pl"), player("2", audio="jp"))
        self.error = error
        self.calls: list[tuple[str, bool]] = []

    def rank(self, players):
        return list(players)

    def resolve(self, entry, *, policy, force_fresh, cancel):
        self.calls.append((entry.online_id, force_fresh))
        if self.error is not None:
            raise self.error
        return ResolvedStream(stream=progressive("http://h/v.mp4"), source=entry, embed=None)


def _xhr(outcome_next: bool) -> ShindenXhrError:
    return ShindenXhrError(
        "x", stage="player_show", category="rate_limited" if not outcome_next else "no_player", reason="r", retry_after=40
    )


@pytest.mark.unit
class TestSessionSources:
    def test_download_order_puts_the_original_audio_first(self):
        assert [e.online_id for e in _SessionSources(_Session()).entries] == ["2", "1"]

    def test_fresh_resolution_is_passed_through(self):
        session = _Session()
        _SessionSources(session).resolve(player("2"), fresh=True, cancel=NEVER)
        assert session.calls == [("2", True)]

    def test_host_needing_javascript_asks_for_interaction(self):
        attempt = SourceAttempt(source=player("1"), host="host.example", error="JavaScriptRequiredError")
        session = _Session(SourcesExhaustedError("x", attempts=(attempt,)))
        with pytest.raises(InteractionNeeded) as exc:
            _SessionSources(session).resolve(player("1"), fresh=False, cancel=NEVER)
        assert (exc.value.scope, exc.value.host, exc.value.online_id) == (SCOPE_HOST, "host.example", "1")

    def test_exhausted_source_is_unavailable(self):
        attempt = SourceAttempt(source=player("1"), host="h", error="NoStreamError")
        with pytest.raises(SourceUnavailable) as exc:
            _SessionSources(_Session(SourcesExhaustedError("x", attempts=(attempt,)))).resolve(
                player("1"), fresh=False, cancel=NEVER
            )
        assert exc.value.transient

    def test_shinden_api_outage_is_transient_with_its_retry_after(self, monkeypatch):
        monkeypatch.setattr(shinden_api, "xhr_outcome", lambda _e: shinden_api.XhrOutcome.SERVICE_UNAVAILABLE)
        with pytest.raises(SourceUnavailable) as exc:
            _SessionSources(_Session(_xhr(False))).resolve(player("1"), fresh=False, cancel=NEVER)
        assert exc.value.transient and exc.value.retry_after == 40

    def test_blocked_xhr_asks_for_interaction(self, monkeypatch):
        monkeypatch.setattr(shinden_api, "xhr_outcome", lambda _e: shinden_api.XhrOutcome.ACCESS_BLOCKED)
        with pytest.raises(InteractionNeeded) as exc:
            _SessionSources(_Session(_xhr(False))).resolve(player("1"), fresh=False, cancel=NEVER)
        assert exc.value.scope == SCOPE_SHINDEN

    def test_blocked_shinden_asks_for_interaction(self):
        with pytest.raises(InteractionNeeded) as exc:
            _SessionSources(_Session(AccessBlockedError("blocked", kind="cloudflare"))).resolve(
                player("1"), fresh=False, cancel=NEVER
            )
        assert exc.value.scope == SCOPE_SHINDEN

    def test_other_engine_errors_are_unavailable_sources(self):
        with pytest.raises(SourceUnavailable):
            _SessionSources(_Session(NoStreamError("x"))).resolve(player("1"), fresh=False, cancel=NEVER)


class _Engine:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def open_episode(self, episode, *, cancel):
        raise self.error

    def set_access(self, access) -> None:
        pass


@pytest.mark.unit
class TestEngineSources:
    def test_blocked_episode_page_asks_for_interaction(self):
        sources = EngineSources(_Engine(AccessBlockedError("cf", kind="cloudflare")), AccessConfig())
        with pytest.raises(InteractionNeeded):
            sources.open(episode(1), grant=None, cancel=NEVER)

    def test_unreadable_episode_page_is_an_unavailable_source(self):
        sources = EngineSources(_Engine(NoStreamError("x")), AccessConfig())
        with pytest.raises(SourceUnavailable):
            sources.open(episode(1), grant=None, cancel=NEVER)
