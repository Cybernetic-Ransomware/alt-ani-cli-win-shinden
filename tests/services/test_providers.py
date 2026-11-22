import re
import threading

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import AccessConfig, EngineConfig
from alt_ani_core.errors import (
    NoStreamError,
    OperationCancelled,
    ParseError,
    PlayerNotFoundError,
    ReloadLimitError,
    SourcesExhaustedError,
    StaleSessionError,
)
from alt_ani_core.events import PhaseStarted
from alt_ani_core.models import (
    EmbedURL,
    EpisodeListing,
    EpisodeRow,
    FallbackPolicy,
    PlayerEntry,
    SearchResults,
    SeriesMetadata,
    SeriesRef,
)
from alt_ani_core.providers.api import StaticEmbeds, url_matches
from alt_ani_core.providers.shinden import ShindenProvider
from alt_ani_core.services.engine import Engine
from alt_ani_core.testing import FakeResponse, InMemoryHistory
from alt_ani_core.transport.api import HttpSession, SessionProfile, TransportError

from .support import EPISODE, EPISODE_URL, route_xhr

_HOST = "static.example"
_REFERER = f"https://{_HOST}/"
_SERIES_URL = f"https://{_HOST}/anime/elfen-lied/"
_EP_URL = f"https://{_HOST}/anime/elfen-lied/1/"
_STATIC_EP = EpisodeRow(number=1.0, title="Ep 1", url=_EP_URL)
_STATIC_REF = SeriesRef(id="static:7", slug="elfen-lied", title="Elfen Lied", url=_SERIES_URL)
_SHINDEN_REF = SeriesRef(
    id="61064", slug="sousou-no-frieren", title="Frieren", url="https://shinden.pl/series/61064-sousou-no-frieren"
)
_SOLVER_ACCESS = AccessConfig(flaresolverr_url="http://solver.invalid:8191/v1", use_flaresolverr=True)
_TIMEOUT = 5.0


def _embed(online_id: str) -> EmbedURL:
    return EmbedURL(url=f"https://h{online_id}.example/e/{online_id}", referer=_REFERER)


class StaticProvider:
    """A site whose episode page lists its embeds outright; every catalogue call is one GET on its own session."""

    name = "static"

    def __init__(self) -> None:
        self.profiles: list[AccessConfig] = []
        self.player_ids: tuple[str, ...] = ("1", "2")
        self.embed_ids: tuple[str, ...] | None = None
        self.unreachable = False

    def owns(self, url: str) -> bool:
        return url_matches(url, hosts=frozenset({_HOST}), path=re.compile(r"/anime/[^/]+(?:/|$)"))

    def session_profile(self, access: AccessConfig, cache_dir) -> SessionProfile:
        self.profiles.append(access)
        if self.unreachable:
            raise TransportError("site down")
        return SessionProfile(name=self.name, headers={"Referer": _REFERER})

    def search(self, client: HttpSession, query: str, *, cancel: CancelToken) -> SearchResults:
        client.get(f"https://{_HOST}/szukaj/{query}")
        return SearchResults(hits=[])

    def series_from_url(self, url: str) -> SeriesRef:
        return _STATIC_REF

    def metadata(self, client: HttpSession, ref: SeriesRef) -> SeriesMetadata:
        client.get(ref.url).raise_for_status()
        return SeriesMetadata(air_date=None, air_date_sort=None, description=ref.title, tags=(), related=())

    def episodes(self, client: HttpSession, ref: SeriesRef, *, cancel: CancelToken) -> EpisodeListing:
        client.get(ref.url).raise_for_status()
        return EpisodeListing(ref=ref, episodes=[_STATIC_EP])

    def load_episode(self, client: HttpSession, episode: EpisodeRow) -> tuple[list[PlayerEntry], StaticEmbeds]:
        client.get(episode.url).raise_for_status()
        players = [PlayerEntry(online_id=i, player=f"Host{i}", lang_audio="jp", lang_subs="pl") for i in self.player_ids]
        embed_ids = self.player_ids if self.embed_ids is None else self.embed_ids
        return players, StaticEmbeds(players, {i: _embed(i) for i in embed_ids})


@pytest.fixture
def static() -> StaticProvider:
    return StaticProvider()


@pytest.fixture
def make_mixed(tmp_path, transport, sink, static):
    """Shinden first, then the static provider; FlareSolverr configured so a stray clearance lookup would show."""
    engines: list[Engine] = []

    def _make(*, access: AccessConfig = _SOLVER_ACCESS) -> Engine:
        engine = Engine(
            EngineConfig(cache_dir=tmp_path / "cache", antibot_delay_sec=0.0, access=access),
            transport=transport,
            history=InMemoryHistory(),
            events=sink,
            providers=[ShindenProvider(xhr_backend="new", antibot_delay_sec=0.0, events=sink), static],
        )
        engines.append(engine)
        return engine

    yield _make
    for engine in engines:
        engine.close()


@pytest.fixture
def clearance_calls(monkeypatch):
    calls: list[AccessConfig] = []

    def get_clearance(access, cache_dir, events=None):
        calls.append(access)

    monkeypatch.setattr("alt_ani_core.transport.clearance.get_clearance", get_clearance)
    return calls


@pytest.fixture
def static_site(transport):
    transport.route(f"https://{_HOST}/", FakeResponse(text="<html></html>"))
    return transport


@pytest.mark.unit
class TestRouting:
    @pytest.mark.parametrize(
        ("url", "provider"),
        [
            ("https://shinden.pl/series/61064-sousou-no-frieren", "shinden"),
            ("  https://shinden.pl/titles/12-one-piece  ", "shinden"),
            (_SERIES_URL, "static"),
        ],
    )
    def test_series_url_goes_to_its_owner(self, make_mixed, transport, url, provider):
        ref = make_mixed().series_from_url(url)

        assert (ref is _STATIC_REF) is (provider == "static")
        assert transport.sessions == []

    @pytest.mark.parametrize(
        "url",
        [
            "https://evilshinden.pl/series/1-fate",
            "https://evil.example/?u=https://shinden.pl/series/1-fate",
            "https://static.example.evil.example/anime/x/",
            "ftp://static.example/anime/x/",
            "https://user@static.example/anime/x/",
            "https://static.example:8080/anime/x/",
            "https://static.example/manga/x/",
            "not a url",
        ],
    )
    def test_unknown_series_url_is_refused_before_any_session(self, make_mixed, transport, url):
        engine = make_mixed()

        with pytest.raises(ParseError, match="Cannot extract series ID"):
            engine.series_from_url(url)
        with pytest.raises(ParseError):
            engine.metadata(SeriesRef(id="1", slug="x", title="X", url=url))
        with pytest.raises(ParseError):
            engine.episodes(SeriesRef(id="1", slug="x", title="X", url=url))
        assert transport.sessions == []

    def test_unknown_episode_url_is_refused_before_any_session(self, make_mixed, transport):
        with pytest.raises(ParseError, match="episode URL"):
            make_mixed().open_episode(EpisodeRow(number=1.0, title="x", url="https://evil.example/episode/1-x/view/2"))
        assert transport.sessions == []

    def test_search_stays_on_the_first_provider(self, make_mixed, transport, clearance_calls, shinden_html):
        transport.route("https://shinden.pl/series", shinden_html("search_frieren.html"))

        assert make_mixed().search("frieren").hits

        assert {r.profile for r in transport.requests} == {"shinden"}

    def test_duplicate_or_missing_providers_are_refused(self, tmp_path, transport, static):
        for providers in ([], [static, static]):
            with pytest.raises(ValueError):
                Engine(EngineConfig(cache_dir=tmp_path), transport=transport, history=InMemoryHistory(), providers=providers)


@pytest.mark.unit
class TestStaticProviderSessions:
    def test_catalogue_and_episode_use_only_the_static_session_and_never_clearance(
        self, make_mixed, static_site, clearance_calls, static
    ):
        engine = make_mixed()

        assert engine.metadata(_STATIC_REF).description == "Elfen Lied"
        assert engine.episodes(_STATIC_REF).episodes == [_STATIC_EP]
        engine.open_episode(_STATIC_EP)

        assert clearance_calls == []
        assert [s.profile.name for s in static_site.sessions] == ["static"]
        assert {r.profile for r in static_site.requests} == {"static"}
        assert static.profiles == [_SOLVER_ACCESS]

    def test_providers_keep_separate_sessions_in_one_generation(self, make_mixed, static_site, clearance_calls, episode_page):
        static_site.route(EPISODE_URL, episode_page())
        engine = make_mixed()

        engine.open_episode(_STATIC_EP)
        engine.open_episode(EPISODE)
        engine.metadata(_STATIC_REF)

        assert [s.profile.name for s in static_site.sessions] == ["static", "shinden"]
        assert len(clearance_calls) == 1

    def test_metadata_many_routes_each_ref_and_marks_unknown_ones_none(
        self, make_mixed, static_site, clearance_calls, shinden_html
    ):
        static_site.route(_SHINDEN_REF.url, shinden_html("series_frieren.html"))
        stray = SeriesRef(id="9", slug="x", title="X", url="https://evil.example/anime/x/")

        found = make_mixed().metadata_many([_STATIC_REF, stray, _SHINDEN_REF])

        assert found[0] is not None and found[0].description == "Elfen Lied"
        assert found[1] is None
        assert found[2] is not None
        assert "https://evil.example/anime/x/" not in static_site.urls

    def test_metadata_many_reads_providers_concurrently(self, make_mixed, static_site, clearance_calls, shinden_html):
        both_in_flight = threading.Barrier(2, timeout=_TIMEOUT)
        shinden_page = shinden_html("series_frieren.html")

        def meet(page):
            def reply(request):
                both_in_flight.wait()
                return page

            return reply

        static_site.route(_STATIC_REF.url, meet("<html></html>"))
        static_site.route(_SHINDEN_REF.url, meet(shinden_page))

        found = make_mixed().metadata_many([_STATIC_REF, _SHINDEN_REF])

        assert None not in found

    def test_metadata_many_keeps_other_providers_when_one_is_unreachable(
        self, make_mixed, static_site, static, clearance_calls, shinden_html
    ):
        static_site.route(_SHINDEN_REF.url, shinden_html("series_frieren.html"))
        static.unreachable = True

        found = make_mixed().metadata_many([_STATIC_REF, _SHINDEN_REF])

        assert found[0] is None
        assert found[1] is not None
        assert _STATIC_REF.url not in static_site.urls

    def test_metadata_many_with_two_providers_still_propagates_cancellation(
        self, make_mixed, static_site, clearance_calls, shinden_html
    ):
        cancel = CancelToken()
        both_in_flight = threading.Barrier(2, timeout=_TIMEOUT)

        def cancelling(request):
            both_in_flight.wait()
            cancel.cancel()
            return "<html></html>"

        def waiting(request):
            both_in_flight.wait()
            return shinden_html("series_frieren.html")

        static_site.route(_STATIC_REF.url, cancelling)
        static_site.route(_SHINDEN_REF.url, waiting)
        engine = make_mixed()

        with pytest.raises(OperationCancelled):
            engine.metadata_many([_STATIC_REF, _SHINDEN_REF], cancel=cancel)

        workers = [s for s in static_site.sessions if s.threads]
        assert len(workers) == 2
        assert all(worker.closed for worker in workers)
        assert engine.closed is False


@pytest.mark.unit
class TestStaticEpisodeSession:
    def test_resolve_extracts_the_listed_embed_with_no_xhr_and_no_antibot_phase(
        self, make_mixed, static_site, clearance_calls, extract_seam, diag, sink
    ):
        session = make_mixed().open_episode(_STATIC_EP)

        resolved = session.resolve(session.sources[0])

        assert resolved.embed == _embed("1")
        assert extract_seam.call_args.args == ("https://h1.example/e/1", _REFERER)
        assert sink.of(PhaseStarted) == []
        assert not [u for u in static_site.urls if "/xhr/" in u]
        assert static_site.urls == [_EP_URL]
        ok_result = diag.resolve_result.call_args
        assert ok_result.args[:2] == ("h1.example", True)
        assert "xhr_backend" not in ok_result.kwargs

    def test_walk_falls_back_to_the_next_static_source(self, make_mixed, static_site, extract_seam, clearance_calls):
        extract_seam.side_effect = [NoStreamError("dead host"), extract_seam.return_value]
        session = make_mixed().open_episode(_STATIC_EP)

        resolved = session.resolve(session.sources[0], candidates=session.sources, policy=FallbackPolicy.WALK)

        assert resolved.source.online_id == "2"

    def test_every_source_failing_is_exhausted_not_a_shared_stop(self, make_mixed, static_site, extract_seam, clearance_calls):
        extract_seam.side_effect = NoStreamError("dead host")
        session = make_mixed().open_episode(_STATIC_EP)

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(session.sources[0], candidates=session.sources, policy=FallbackPolicy.WALK)

        assert [a.source.online_id for a in exc_info.value.attempts] == ["1", "2"]

    def test_probe_hosts_learns_hosts_without_requests(self, make_mixed, static_site, clearance_calls):
        session = make_mixed().open_episode(_STATIC_EP)

        assert session.probe_hosts(session.sources) is None
        assert set(session.known_sources) == {"1", "2"}
        assert static_site.urls == [_EP_URL]

    def test_reload_rereads_the_page_once_on_the_static_session(self, make_mixed, static_site, clearance_calls):
        session = make_mixed().open_episode(_STATIC_EP)

        session.reload()

        assert static_site.urls == [_EP_URL, _EP_URL]
        with pytest.raises(ReloadLimitError):
            session.reload()

    def test_a_page_whose_players_and_embeds_disagree_does_not_open(self, make_mixed, static_site, static, clearance_calls):
        static.embed_ids = ("1",)

        with pytest.raises(ParseError) as exc_info:
            make_mixed().open_episode(_STATIC_EP)

        assert exc_info.value.code == "episode.embeds_mismatch"

    def test_a_source_from_before_a_reload_is_a_classified_error(
        self, make_mixed, static_site, static, extract_seam, clearance_calls
    ):
        session = make_mixed().open_episode(_STATIC_EP)
        stale = session.sources[1]
        static.player_ids = ("1", "3")
        session.reload()

        with pytest.raises(PlayerNotFoundError):
            session.resolve(stale)
        extract_seam.assert_not_called()

    def test_set_access_makes_a_static_session_stale_and_closes_its_http_session(
        self, make_mixed, static_site, extract_seam, clearance_calls
    ):
        engine = make_mixed()
        session = engine.open_episode(_STATIC_EP)

        engine.set_access(AccessConfig(use_flaresolverr=False))

        with pytest.raises(StaleSessionError):
            session.resolve(session.sources[0])
        assert static_site.sessions[0].closed is True
        extract_seam.assert_not_called()

    def test_cancelled_operation_sends_nothing(self, make_mixed, static_site, extract_seam, clearance_calls):
        session = make_mixed().open_episode(_STATIC_EP)
        token = CancelToken()
        token.cancel()

        with pytest.raises(OperationCancelled):
            session.resolve(session.sources[0], cancel=token)
        extract_seam.assert_not_called()


@pytest.mark.unit
class TestShindenUnchangedNextToAnotherProvider:
    def test_shinden_episode_still_goes_through_the_player_api(
        self, make_mixed, transport, episode_page, extract_seam, sink, clearance_calls
    ):
        transport.route(EPISODE_URL, episode_page())
        route_xhr(transport)
        session = make_mixed().open_episode(EPISODE)

        session.resolve(session.sources[0])

        assert [u for u in transport.urls if "/xhr/" in u]
        assert sink.of(PhaseStarted)
        assert {r.profile for r in transport.requests} == {"shinden"}
