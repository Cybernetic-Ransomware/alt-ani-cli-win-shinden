from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest
from alt_ani_core.errors import ShindenError
from alt_ani_core.models import EpisodeListing, RelatedSeries, SeriesHit, SeriesMetadata, SeriesRef
from alt_ani_core.transport.api import NetworkError

from alt_ani_cli import __version__
from alt_ani_cli.args import CliOptions
from alt_ani_cli.content import CONTENT
from alt_ani_cli.flow.handlers import HANDLERS, _prefetch_series_metadata, _sorted_by_date_desc
from alt_ani_cli.flow.state import Screen, _BackSentinel

from .support import (
    EP1,
    EP2,
    EP3,
    EP4,
    EP5,
    EP6,
    EP7,
    PLAYER,
    PLAYER2,
    SERIES_HIT,
    SERIES_REF,
    FakeDownloads,
    batch_state,
    fake_engine,
    make_state,
    noop_spinner,
)

_PROG = CONTENT["progress"]
_EMPTY_META = SeriesMetadata(None, None, "", (), ())
_SERIES_URL = "https://shinden.pl/series/1-fate"


def _numbered_ref(n: int) -> SeriesRef:
    return SeriesRef(id=str(n), slug=f"s{n}", title=f"S{n}", url=f"http://shinden.pl/series/{n}-s{n}")


def _listing_state(listing: EpisodeListing, **fields):
    state = make_state(ref=SERIES_REF, **fields)
    state.engine.episodes.return_value = listing
    return state


@pytest.mark.unit
class TestHandleStartMode:
    def test_esc_returns_back(self):
        state = make_state()
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value=None):
            result = HANDLERS[Screen.START_MODE](state)
        assert isinstance(result, _BackSentinel)

    def test_quit_returns_none(self):
        state = make_state()
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value="quit"):
            result = HANDLERS[Screen.START_MODE](state)
        assert result is None

    def test_search_returns_search_query(self):
        state = make_state()
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value="search"):
            result = HANDLERS[Screen.START_MODE](state)
        assert result is Screen.SEARCH_QUERY

    def test_args_resume_skips_menu(self):
        state = make_state(options=CliOptions(resume=True))
        with patch("alt_ani_cli.ui.menus.select_start_mode") as mock_menu:
            result = HANDLERS[Screen.START_MODE](state)
        mock_menu.assert_not_called()
        assert result is Screen.RESUME_PICK

    def test_args_url_skips_menu_and_sets_ref(self):
        state = make_state(options=CliOptions(url="http://shinden.pl/series/1-fate"))
        state.engine.series_from_url.return_value = SERIES_REF
        with patch("alt_ani_cli.ui.menus.select_start_mode") as mock_menu:
            result = HANDLERS[Screen.START_MODE](state)
        mock_menu.assert_not_called()
        state.engine.series_from_url.assert_called_once_with("http://shinden.pl/series/1-fate")
        assert result is Screen.FETCH_EPISODES
        assert state.ref == SERIES_REF

    def test_args_query_skips_menu(self):
        state = make_state(options=CliOptions(query=("fate", "strange")))
        result = HANDLERS[Screen.START_MODE](state)
        assert result is Screen.SERIES_PICK
        assert state.query == "fate strange"

    def test_version_signal_shows_modal_and_rerenders(self):
        state = make_state()
        signals = iter(["version", None])
        with (
            patch("alt_ani_cli.ui.menus.select_start_mode", side_effect=lambda **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.START_MODE](state)
        mock_modal.assert_called_once()
        assert __version__ in mock_modal.call_args[0][1]
        assert isinstance(result, _BackSentinel)


@pytest.mark.unit
class TestStartModeHistoryOptions:
    def _run(self, choice, watch=(), downloads=(), state=None):
        state = state or make_state()
        for ref in watch:
            state.engine.history.record_watch(ref, 1.0)
        for ref in downloads:
            state.engine.history.record_download(ref, 1.0)
        with patch("alt_ani_cli.ui.menus.select_start_mode", return_value=choice) as mock_menu:
            result = HANDLERS[Screen.START_MODE](state)
        return state, result, mock_menu.call_args.kwargs

    def test_menu_receives_both_counts(self):
        _, _, kwargs = self._run(
            None, watch=[_numbered_ref(n) for n in range(22)], downloads=[_numbered_ref(n) for n in range(3)]
        )
        assert kwargs == {"watch_count": 22, "download_count": 3}

    def test_resume_watch_goes_to_resume_pick(self):
        _, result, _ = self._run("resume_watch", watch=[SERIES_REF])
        assert result is Screen.RESUME_PICK

    def test_resume_download_goes_to_download_resume_pick(self):
        _, result, _ = self._run("resume_download", downloads=[SERIES_REF])
        assert result is Screen.DOWNLOAD_RESUME_PICK

    @pytest.mark.parametrize(
        "choice, expected",
        [("search", Screen.SEARCH_QUERY), ("url", Screen.URL_INPUT), ("resume_watch", Screen.RESUME_PICK)],
    )
    def test_leaving_download_resume_clears_its_state(self, choice, expected):
        state = make_state(resume_mode="download", downloaded_eps={1.0}, episode_action="download")
        _, result, _ = self._run(choice, state=state)
        assert result is expected
        assert state.resume_mode is None
        assert state.downloaded_eps == set()
        assert state.episode_action is None

    def test_cached_download_action_outside_resume_mode_is_kept(self):
        state = make_state(episode_action="download")
        self._run("search", state=state)
        assert state.episode_action == "download"

    def test_args_resume_with_download_skips_menu_to_download_resume(self):
        state = make_state(options=CliOptions(resume=True, download=True))
        assert HANDLERS[Screen.START_MODE](state) is Screen.DOWNLOAD_RESUME_PICK


@pytest.mark.unit
class TestHandleSearchQuery:
    def test_esc_returns_back(self):
        state = make_state()
        with patch("alt_ani_cli.ui.menus.prompt_search_query", return_value=None):
            result = HANDLERS[Screen.SEARCH_QUERY](state)
        assert isinstance(result, _BackSentinel)

    def test_query_set_and_returns_series_pick(self):
        state = make_state()
        with patch("alt_ani_cli.ui.menus.prompt_search_query", return_value="fate"):
            result = HANDLERS[Screen.SEARCH_QUERY](state)
        assert result is Screen.SERIES_PICK
        assert state.query == "fate"


@pytest.mark.unit
class TestHandleUrlInput:
    def test_esc_returns_back(self):
        state = make_state()
        with patch("alt_ani_cli.ui.menus.prompt_url", return_value=None):
            result = HANDLERS[Screen.URL_INPUT](state)
        assert isinstance(result, _BackSentinel)
        state.engine.series_from_url.assert_not_called()

    def test_url_sets_ref_and_leaves_download_resume(self):
        state = make_state(resume_mode="download", downloaded_eps={1.0}, episode_action="download", last_ep=4.0)
        state.engine.series_from_url.return_value = SERIES_REF
        with patch("alt_ani_cli.ui.menus.prompt_url", return_value=SERIES_REF.url):
            result = HANDLERS[Screen.URL_INPUT](state)
        state.engine.series_from_url.assert_called_once_with(SERIES_REF.url)
        assert result is Screen.FETCH_EPISODES
        assert (state.ref, state.last_ep) == (SERIES_REF, 0.0)
        assert (state.resume_mode, state.downloaded_eps, state.episode_action) == (None, set(), None)


@pytest.mark.unit
class TestHandleResumePickModes:
    def test_resume_watch_keeps_last_ep(self):
        state = make_state(resume_mode="download", downloaded_eps={1.0}, episode_action="download")
        state.engine.history.record_watch(SERIES_REF, 5.0)
        with patch("alt_ani_cli.ui.menus.select_series_from_history", return_value=(SERIES_REF, 5.0)) as mock_pick:
            result = HANDLERS[Screen.RESUME_PICK](state)
        assert mock_pick.call_args.args[0] == [(SERIES_REF, 5.0)]
        assert result is Screen.FETCH_EPISODES
        assert state.ref == SERIES_REF
        assert state.last_ep == 5.0
        assert state.resume_mode == "watch"
        assert state.downloaded_eps == set()
        assert state.episode_action is None

    def test_resume_watch_empty_history_returns_back(self):
        state = make_state()
        with (
            patch("alt_ani_cli.ui.menus.select_series_from_history") as mock_pick,
            patch("alt_ani_cli.ui.progress.error") as mock_err,
        ):
            result = HANDLERS[Screen.RESUME_PICK](state)
        assert isinstance(result, _BackSentinel)
        mock_pick.assert_not_called()
        mock_err.assert_called_once_with(_PROG["history_empty"])

    def test_resume_download_sets_download_state(self):
        state = make_state(last_ep=3.0)
        state.engine.history.record_download(SERIES_REF, 1.0)
        state.engine.history.record_download(SERIES_REF, 2.0)
        with patch(
            "alt_ani_cli.ui.menus.select_series_from_download_history", side_effect=lambda entries: entries[0]
        ) as mock_pick:
            result = HANDLERS[Screen.DOWNLOAD_RESUME_PICK](state)
        assert mock_pick.call_args.args[0] == [(SERIES_REF, frozenset({1.0, 2.0}))]
        assert result is Screen.FETCH_EPISODES
        assert state.ref == SERIES_REF
        assert state.resume_mode == "download"
        assert state.downloaded_eps == {1.0, 2.0}
        assert state.episode_action == "download"
        assert state.last_ep == 0.0

    def test_resume_download_esc_returns_back(self):
        state = make_state()
        state.engine.history.record_download(SERIES_REF, 1.0)
        with patch("alt_ani_cli.ui.menus.select_series_from_download_history", return_value=None):
            result = HANDLERS[Screen.DOWNLOAD_RESUME_PICK](state)
        assert isinstance(result, _BackSentinel)
        assert state.resume_mode is None
        assert state.episode_action is None

    def test_resume_download_empty_history_returns_back(self):
        state = make_state()
        with patch("alt_ani_cli.ui.progress.error") as mock_err:
            result = HANDLERS[Screen.DOWNLOAD_RESUME_PICK](state)
        assert isinstance(result, _BackSentinel)
        mock_err.assert_called_once()


@pytest.mark.unit
class TestHandleSeriesPick:
    def _state(self, hits=(SERIES_HIT,)):
        state = make_state(query="fate")
        state.engine.search.return_value = list(hits)
        state.engine.series_from_url.return_value = SERIES_REF
        return state

    def test_esc_returns_back(self):
        state = self._state()
        with (
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={}),
            patch("alt_ani_cli.ui.menus.select_series_once", return_value=("back", None)),
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)
        state.engine.search.assert_called_once_with("fate")
        assert isinstance(result, _BackSentinel)

    def test_no_query_goes_to_search_query(self):
        state = make_state()
        assert HANDLERS[Screen.SERIES_PICK](state) is Screen.SEARCH_QUERY
        state.engine.search.assert_not_called()

    def test_no_results_returns_back(self):
        state = self._state(hits=())
        with patch("alt_ani_cli.ui.progress.error") as mock_err:
            result = HANDLERS[Screen.SERIES_PICK](state)
        assert isinstance(result, _BackSentinel)
        mock_err.assert_called_once_with(_PROG["no_results"].format(query=repr("fate")))

    def test_pick_sets_ref_and_returns_fetch(self):
        hit = replace(SERIES_HIT, title="Fate/stay night")
        state = self._state(hits=(hit,))
        with (
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={}),
            patch("alt_ani_cli.ui.menus.select_series_once", return_value=("pick", hit)),
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)
        state.engine.series_from_url.assert_called_once_with(hit.url)
        assert result is Screen.FETCH_EPISODES
        assert state.ref == replace(SERIES_REF, title="Fate/stay night")

    def test_sort_signal_toggles_order(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_a = SeriesMetadata(air_date="01.01.2018", air_date_sort=(2018, 1, 1), description="", tags=(), related=())
        meta_b = SeriesMetadata(air_date="01.01.2022", air_date_sort=(2022, 1, 1), description="", tags=(), related=())

        received_hits: list[list] = []
        signals = iter([("sort", 0), ("sort", 0), ("pick", hit_b)])

        def _once(hits, metadata=None, **kw):
            received_hits.append(list(hits))
            return next(signals)

        state = self._state(hits=(hit_a, hit_b))
        with (
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={"1": meta_a, "2": meta_b}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=_once),
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        assert result is Screen.FETCH_EPISODES
        assert [[h.id for h in hits] for hits in received_hits] == [["1", "2"], ["2", "1"], ["1", "2"]]

    def test_desc_signal_calls_show_modal_text(self):
        meta = SeriesMetadata(air_date=None, air_date_sort=None, description="Great show", tags=(), related=())
        signals = iter([("desc", 0), ("back", None)])

        state = self._state()
        with (
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={SERIES_HIT.id: meta}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=lambda *a, **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        mock_modal.assert_called_once()
        assert mock_modal.call_args.args[1] == "Great show"
        assert isinstance(result, _BackSentinel)

    def test_tags_signal_calls_show_modal_text(self):
        meta = SeriesMetadata(air_date=None, air_date_sort=None, description="", tags=("Action", "Comedy"), related=())
        signals = iter([("tags", 0), ("back", None)])

        state = self._state()
        with (
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={SERIES_HIT.id: meta}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=lambda *a, **kw: next(signals)),
            patch("alt_ani_cli.ui.menus.show_modal_text") as mock_modal,
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        mock_modal.assert_called_once()
        assert mock_modal.call_args.args[1] == "• Action\n• Comedy"
        assert isinstance(result, _BackSentinel)

    def test_related_signal_substitutes_hit(self):
        related = RelatedSeries(
            id="99", slug="zero", title="Fate/Zero", url="http://shinden.pl/series/99-zero", relation="Prequel"
        )
        meta = SeriesMetadata(air_date=None, air_date_sort=None, description="", tags=(), related=(related,))

        received_hits: list[list] = []
        signals = iter([("related", 0), ("back", None)])

        def _once(hits, metadata=None, **kw):
            received_hits.append(list(hits))
            return next(signals)

        state = self._state()
        with (
            patch("alt_ani_cli.flow.handlers._prefetch_series_metadata", return_value={SERIES_HIT.id: meta}),
            patch("alt_ani_cli.ui.menus.select_series_once", side_effect=_once),
            patch("alt_ani_cli.ui.menus.pick_related", return_value=related) as mock_related,
        ):
            result = HANDLERS[Screen.SERIES_PICK](state)

        mock_related.assert_called_once_with((related,))
        assert isinstance(result, _BackSentinel)
        assert len(received_hits) == 2
        assert received_hits[1][0].id == "99"


@pytest.mark.unit
class TestSortedByDateDesc:
    def test_sorts_by_year_descending(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_a = SeriesMetadata(air_date="2018", air_date_sort=(2018, 1, 1), description="", tags=(), related=())
        meta_b = SeriesMetadata(air_date="2022", air_date_sort=(2022, 6, 15), description="", tags=(), related=())

        result = _sorted_by_date_desc([hit_a, hit_b], {"1": meta_a, "2": meta_b})
        assert [h.id for h in result] == ["2", "1"]

    def test_no_air_date_sorts_last(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_a = SeriesMetadata(air_date=None, air_date_sort=None, description="", tags=(), related=())
        meta_b = SeriesMetadata(air_date="2022", air_date_sort=(2022, 6, 15), description="", tags=(), related=())

        result = _sorted_by_date_desc([hit_a, hit_b], {"1": meta_a, "2": meta_b})
        assert result[0].id == "2"
        assert result[-1].id == "1"

    def test_missing_metadata_sorts_last(self):
        hit_a = SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a")
        hit_b = SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b")
        meta_b = SeriesMetadata(air_date="2020", air_date_sort=(2020, 3, 1), description="", tags=(), related=())

        result = _sorted_by_date_desc([hit_a, hit_b], {"2": meta_b})
        assert result[0].id == "2"
        assert result[-1].id == "1"

    def test_equal_dates_preserve_relative_order(self):
        hits = [SeriesHit(id=str(i), slug=f"s{i}", title=f"S{i}", url=f"http://shinden.pl/series/{i}-s{i}") for i in range(3)]
        same_meta = SeriesMetadata(air_date="2020", air_date_sort=(2020, 1, 1), description="", tags=(), related=())
        metadata = {str(i): same_meta for i in range(3)}

        result = _sorted_by_date_desc(hits, metadata)
        assert [h.id for h in result] == ["0", "1", "2"]


@pytest.mark.unit
class TestPrefetchSeriesMetadata:
    def _engine(self, *metas):
        engine = fake_engine()
        engine.series_from_url.side_effect = lambda url: SeriesRef(id=url, slug="s", title="T", url=url)
        engine.metadata_many.return_value = list(metas)
        return engine

    def test_returns_metadata_for_all_hits(self):
        meta = SeriesMetadata(air_date="01.01.2020", air_date_sort=(2020, 1, 1), description="", tags=(), related=())
        engine = self._engine(meta)
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            result = _prefetch_series_metadata(engine, [SERIES_HIT])
        assert result == {SERIES_HIT.id: meta}

    def test_passes_parsed_refs_to_the_engine(self):
        engine = self._engine(_EMPTY_META)
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            _prefetch_series_metadata(engine, [SERIES_HIT])
        engine.series_from_url.assert_called_once_with(SERIES_HIT.url)
        engine.metadata_many.assert_called_once_with([SeriesRef(id=SERIES_HIT.url, slug="s", title="T", url=SERIES_HIT.url)])

    def test_unread_series_falls_back_and_warns_once_with_titles(self):
        hits = [_numbered_ref(n) for n in range(3)]
        hits = [SeriesHit(id=r.id, slug=r.slug, title=r.title, url=r.url) for r in hits]
        meta = SeriesMetadata(air_date="2020", air_date_sort=(2020, 1, 1), description="", tags=(), related=())
        engine = self._engine(None, meta, None)
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(engine, hits)
        assert result == {"0": _EMPTY_META, "1": meta, "2": _EMPTY_META}
        mock_warn.assert_called_once_with(CONTENT["menu"]["series"]["metadata_fetch_failed"].format(titles="S0, S2"))

    def test_unparsable_hit_url_falls_back_and_warns(self):
        engine = self._engine()
        engine.series_from_url.side_effect = ShindenError("bad url")
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(engine, [SERIES_HIT])
        engine.metadata_many.assert_called_once_with([])
        assert result == {SERIES_HIT.id: _EMPTY_META}
        mock_warn.assert_called_once()

    def test_http_error_falls_back_and_warns(self, make_engine, transport):
        transport.route(_SERIES_URL, NetworkError("timeout"))
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(make_engine(), [SERIES_HIT])
        assert result == {SERIES_HIT.id: _EMPTY_META}
        mock_warn.assert_called_once()

    def test_shinden_error_falls_back_and_warns(self, make_engine):
        with (
            patch("alt_ani_core.shinden.metadata.fetch_series_metadata", side_effect=ShindenError("age gate")),
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(make_engine(), [SERIES_HIT])
        assert result[SERIES_HIT.id].air_date is None
        mock_warn.assert_called_once()

    def test_series_page_drift_falls_back_and_warns(self, make_engine, transport):
        transport.route(_SERIES_URL, "<html><body><div class='sh-tp'></div></body></html>")
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(make_engine(), [SERIES_HIT])
        assert result[SERIES_HIT.id] == _EMPTY_META
        mock_warn.assert_called_once()

    def test_real_series_page_is_parsed(self, make_engine, transport, shinden_html):
        transport.route(_SERIES_URL, shinden_html("series_frieren.html"))
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = _prefetch_series_metadata(make_engine(), [SERIES_HIT])
        assert result[SERIES_HIT.id].air_date_sort is not None
        mock_warn.assert_not_called()

    def test_unexpected_error_propagates(self, make_engine, transport):
        transport.route(_SERIES_URL, ValueError("unexpected"))
        with (
            patch("alt_ani_cli.ui.progress.spinner", noop_spinner),
            pytest.raises(ValueError, match="unexpected"),
        ):
            _prefetch_series_metadata(make_engine(), [SERIES_HIT])

    def test_empty_hits_returns_empty_dict(self):
        engine = fake_engine()
        assert _prefetch_series_metadata(engine, []) == {}
        engine.metadata_many.assert_not_called()

    def test_all_hits_have_entry_in_result(self):
        hits = [
            SeriesHit(id="1", slug="a", title="A", url="http://shinden.pl/series/1-a"),
            SeriesHit(id="2", slug="b", title="B", url="http://shinden.pl/series/2-b"),
            SeriesHit(id="3", slug="c", title="C", url="http://shinden.pl/series/3-c"),
        ]
        engine = self._engine(_EMPTY_META, _EMPTY_META, _EMPTY_META)
        with patch("alt_ani_cli.ui.progress.spinner", noop_spinner):
            result = _prefetch_series_metadata(engine, hits)
        assert set(result.keys()) == {"1", "2", "3"}

    def test_spinner_is_shown(self):
        spinner_calls = []

        @contextmanager
        def _recording_spinner(msg):
            spinner_calls.append(msg)
            yield

        with patch("alt_ani_cli.ui.progress.spinner", _recording_spinner):
            _prefetch_series_metadata(self._engine(_EMPTY_META), [SERIES_HIT])
        assert len(spinner_calls) == 1


@pytest.mark.unit
class TestHandleFetchEpisodes:
    """Single centralized diagnostics point for series_selected — covers search/url/resume alike."""

    def test_logs_series_selected_with_finalized_ref(self):
        final_ref = replace(SERIES_REF, title="Fate (final)")
        state = _listing_state(EpisodeListing(final_ref, [EP1]), last_ep=0.0)
        with patch("alt_ani_core.diagnostics.series_selected") as mock_diag:
            result = HANDLERS[Screen.FETCH_EPISODES](state)

        state.engine.episodes.assert_called_once_with(SERIES_REF)
        assert result is Screen.EPISODES_PICK
        assert state.ref == final_ref
        assert state.episodes == [EP1]
        mock_diag.assert_called_once_with(final_ref.id, final_ref.title)

    def test_no_episodes_returns_to_series_pick(self):
        state = _listing_state(EpisodeListing(SERIES_REF, []))
        with patch("alt_ani_cli.ui.progress.error") as mock_err:
            assert HANDLERS[Screen.FETCH_EPISODES](state) is Screen.SERIES_PICK
        mock_err.assert_called_once_with(_PROG["no_episodes"])


@pytest.mark.unit
class TestHandleEpisodesPick:
    def test_esc_returns_back(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2])
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=None):
            result = HANDLERS[Screen.EPISODES_PICK](state)
        assert isinstance(result, _BackSentinel)

    def test_empty_selection_warns_and_returns_back(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2])
        with (
            patch("alt_ani_cli.ui.menus.select_episodes", return_value=[]),
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            result = HANDLERS[Screen.EPISODES_PICK](state)
        assert isinstance(result, _BackSentinel)
        mock_warn.assert_called_once_with(_PROG["no_episodes_picked"])

    def test_pick_sets_targets(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2])
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP1]):
            result = HANDLERS[Screen.EPISODES_PICK](state)
        assert result is Screen.EPISODE_DISPATCH
        assert state.targets == [EP1]
        assert state.ep_idx == 0

    def test_watched_numbers_passed_to_menu(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2], completed_eps={1.0})
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        _, kwargs = mock_sel.call_args
        assert kwargs.get("watched_numbers") == {1.0}

    def test_resume_passes_full_list_with_cursor_on_first_unwatched(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2, EP3], last_ep=2.0)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP3]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        args, kwargs = mock_sel.call_args
        assert args[0] == [EP1, EP2, EP3]
        assert kwargs.get("default_index") == 2
        assert kwargs.get("watched_numbers") == {1.0, 2.0}

    def test_resume_all_watched_puts_cursor_on_last_episode(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2, EP3], last_ep=3.0)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP3]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        args, kwargs = mock_sel.call_args
        assert args[0] == [EP1, EP2, EP3]
        assert kwargs.get("default_index") == 2
        assert kwargs.get("watched_numbers") == {1.0, 2.0, 3.0}

    def test_no_resume_has_no_default_index(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2], completed_eps={1.0})
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        _, kwargs = mock_sel.call_args
        assert kwargs.get("default_index") is None
        assert kwargs.get("watched_numbers") == {1.0}

    def test_cli_episode_arg_skips_menu(self):
        state = make_state(options=CliOptions(episode="1"), ref=SERIES_REF, episodes=[EP1, EP2])
        with patch("alt_ani_cli.ui.menus.select_episodes") as mock_sel:
            result = HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_not_called()
        assert result is Screen.EPISODE_DISPATCH
        assert state.targets == [EP1]


@pytest.mark.unit
class TestEpisodeRangeErrors:
    def _run(self, episode: str):
        state = make_state(options=CliOptions(episode=episode), ref=SERIES_REF, episodes=[EP1, EP2])
        with (
            patch("alt_ani_cli.ui.menus.select_episodes") as mock_sel,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
            patch("alt_ani_cli.ui.progress.error") as mock_error,
        ):
            result = HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_not_called()
        assert isinstance(result, _BackSentinel)
        assert state.targets == []
        return state, mock_warn, mock_error

    def test_unrecognised_format_warns_then_reports_not_found(self):
        state, mock_warn, mock_error = self._run("abc")
        mock_warn.assert_called_once_with(_PROG["range_unrecognized"].format(range=repr("abc")))
        mock_error.assert_called_once_with(_PROG["range_not_found"].format(range=repr("abc")))
        assert state.episode_arg_consumed is False

    def test_valid_range_without_episodes_reports_not_found_only(self):
        _, mock_warn, mock_error = self._run("9")
        mock_warn.assert_not_called()
        mock_error.assert_called_once_with(_PROG["range_not_found"].format(range=repr("9")))


@pytest.mark.unit
class TestEpisodesPickDownloadResume:
    def _pick(self, episodes, downloaded):
        state = make_state(ref=SERIES_REF, episodes=episodes, resume_mode="download", downloaded_eps=set(downloaded))
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[episodes[-1]]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        return mock_sel.call_args

    def test_passes_downloaded_numbers_and_cursor_on_first_missing(self):
        args, kwargs = self._pick([EP1, EP2, EP3], {1.0, 2.0})
        assert args[0] == [EP1, EP2, EP3]
        assert kwargs["downloaded_numbers"] == {1.0, 2.0}
        assert kwargs["default_index"] == 2
        assert kwargs["watched_numbers"] == set()

    def test_gap_in_downloads_puts_cursor_on_gap_not_after_max(self):
        _, kwargs = self._pick([EP1, EP2, EP3, EP4, EP5, EP6], {1.0, 2.0, 3.0, 5.0})
        assert kwargs["default_index"] == 3

    def test_all_downloaded_shows_full_list_with_cursor_on_last(self):
        args, kwargs = self._pick([EP1, EP2, EP3], {1.0, 2.0, 3.0})
        assert args[0] == [EP1, EP2, EP3]
        assert kwargs["default_index"] == 2

    def test_watch_resume_cursor_and_watched_ignore_downloads(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2, EP3], last_ep=1.0, resume_mode="watch", downloaded_eps={2.0})
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        kwargs = mock_sel.call_args.kwargs
        assert kwargs["watched_numbers"] == {1.0}
        assert kwargs["default_index"] == 1

    def test_manual_download_outside_resume_marks_episode_without_moving_cursor(self):
        state = make_state(ref=SERIES_REF, episodes=[EP1, EP2, EP3], downloaded_eps={1.0})
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP2]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        kwargs = mock_sel.call_args.kwargs
        assert kwargs["downloaded_numbers"] == {1.0}
        assert kwargs["watched_numbers"] == set()
        assert kwargs["default_index"] is None


@pytest.mark.unit
class TestEpisodeArgConsumedOnce:
    def _state(self, **fields):
        return make_state(ref=SERIES_REF, episodes=[EP4, EP5, EP6, EP7], options=CliOptions(episode="4-7"), **fields)

    def test_first_entry_auto_selects_range_without_picker(self):
        state = self._state()
        with patch("alt_ani_cli.ui.menus.select_episodes") as mock_sel:
            result = HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_not_called()
        assert result is Screen.EPISODE_DISPATCH
        assert state.targets == [EP4, EP5, EP6, EP7]
        assert state.episode_arg_consumed is True

    def test_cancel_on_existing_file_then_shows_picker(self):
        state = self._state()
        HANDLERS[Screen.EPISODES_PICK](state)
        ready = batch_state(state.targets, options=state.options, engine=state.engine, episodes=state.episodes)
        ready.episode_arg_consumed = state.episode_arg_consumed
        fake = FakeDownloads(existing={4.0})
        with (
            patch("alt_ani_cli.flow.handlers.download.run", side_effect=fake),
            patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value="cancel"),
            patch("alt_ani_cli.ui.progress.warn"),
        ):
            result = HANDLERS[Screen.RUN_ACTION](ready)
        assert result is Screen.EPISODES_PICK
        assert ready.overwrite_existing_batch is False
        assert ready.engine.history.download_calls == []
        assert fake.outcomes == [("kept", 4.0)]
        assert ready.ep_idx == 0

        with (
            patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP5]) as mock_sel,
            patch("alt_ani_cli.flow.handlers.parse_range") as mock_parse,
        ):
            result = HANDLERS[Screen.EPISODES_PICK](ready)
        mock_sel.assert_called_once()
        mock_parse.assert_not_called()
        assert result is Screen.EPISODE_DISPATCH
        assert ready.targets == [EP5]

    def test_esc_from_player_pick_then_shows_picker(self):
        state = self._state()
        HANDLERS[Screen.EPISODES_PICK](state)
        state.players = [PLAYER, PLAYER2]
        with patch("alt_ani_cli.ui.menus.select_player_once", return_value=("back", None)):
            assert HANDLERS[Screen.PLAYER_PICK](state) is Screen.EPISODES_PICK
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=None) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        mock_sel.assert_called_once()

    def test_new_series_reapplies_episode_arg(self):
        state = self._state(episode_arg_consumed=True)
        state.engine.episodes.return_value = EpisodeListing(SERIES_REF, [EP4, EP5, EP6, EP7])
        HANDLERS[Screen.FETCH_EPISODES](state)
        assert state.episode_arg_consumed is False
        with patch("alt_ani_cli.ui.menus.select_episodes") as mock_sel:
            assert HANDLERS[Screen.EPISODES_PICK](state) is Screen.EPISODE_DISPATCH
        mock_sel.assert_not_called()

    def test_without_episode_arg_picker_always_shown(self):
        state = make_state(ref=SERIES_REF, episodes=[EP4, EP5])
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP4]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
            HANDLERS[Screen.EPISODES_PICK](state)
        assert mock_sel.call_count == 2
        assert state.episode_arg_consumed is False


_PARTIAL_LISTING = EpisodeListing(SERIES_REF, [EP2, EP3], pages_fetched=30, pages_total=40, incomplete_reason="page_limit")


@pytest.mark.unit
class TestIncompleteEpisodeList:
    """A partial list may be browsed manually, but nothing may be picked from it automatically."""

    def test_fetch_marks_state_and_warns(self):
        state = _listing_state(_PARTIAL_LISTING)
        with patch("alt_ani_cli.ui.progress.warn") as mock_warn:
            assert HANDLERS[Screen.FETCH_EPISODES](state) is Screen.EPISODES_PICK
        assert state.episodes_complete is False
        assert "30/40" in mock_warn.call_args.args[0]

    def test_complete_fetch_resets_flag(self):
        state = _listing_state(EpisodeListing(SERIES_REF, [EP1]), episodes_complete=False)
        HANDLERS[Screen.FETCH_EPISODES](state)
        assert state.episodes_complete is True

    def test_episode_arg_falls_back_to_manual_picker(self):
        state = make_state(options=CliOptions(episode="1-3"), ref=SERIES_REF, episodes=[EP2, EP3], episodes_complete=False)
        with (
            patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP3]) as mock_sel,
            patch("alt_ani_cli.ui.progress.warn") as mock_warn,
        ):
            assert HANDLERS[Screen.EPISODES_PICK](state) is Screen.EPISODE_DISPATCH
        mock_sel.assert_called_once()
        assert state.targets == [EP3]
        assert state.episode_arg_consumed is True
        assert "'1-3'" in mock_warn.call_args.args[0]

    def test_watch_resume_has_no_auto_anchor(self):
        state = make_state(ref=SERIES_REF, episodes=[EP2, EP3], episodes_complete=False, last_ep=2.0)
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP3]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        kwargs = mock_sel.call_args.kwargs
        assert kwargs["default_index"] is None
        assert kwargs["watched_numbers"] == {2.0}

    def test_download_resume_has_no_auto_anchor(self):
        state = make_state(
            ref=SERIES_REF, episodes=[EP2, EP3], episodes_complete=False, resume_mode="download", downloaded_eps={2.0}
        )
        with patch("alt_ani_cli.ui.menus.select_episodes", return_value=[EP3]) as mock_sel:
            HANDLERS[Screen.EPISODES_PICK](state)
        assert mock_sel.call_args.kwargs["default_index"] is None
        assert mock_sel.call_args.kwargs["downloaded_numbers"] == {2.0}
