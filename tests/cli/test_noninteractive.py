"""Unattended runs end to end: a real Engine on a fake shinden.pl, with only the extractor, player and downloader stubbed."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from alt_ani_core import diagnostics
from alt_ani_core.config import GUEST_AUTH, SHINDEN_API_BASE, SHINDEN_XHR_BASE, ExtractOptions
from alt_ani_core.errors import DownloadFailedError, FilterMismatchError, IncompleteEpisodeListError, NoStreamError
from alt_ani_core.events import HostDeferred, SourceAttemptFailed, XhrFallback
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.models import ExtractionFailure, SeriesHit
from alt_ani_core.services.engine import Engine
from alt_ani_core.testing import FakeResponse, InMemoryHistory, make_stream
from alt_ani_core.transport.api import NetworkError, TransportTimeout

from alt_ani_cli.noninteractive import run

from .support import REF, SERIES_URL, TEST_TOKEN, episode, episodes_html, extracted_urls, options, player

_EP1, _EP2 = episode(1), episode(2)
_EPISODES = [episode(n) for n in (1, 2, 3, 4, 5)]
_PLAYER_A = player("11", "PlayerA")
_PLAYER_B = player("22", "PlayerB")


def _events(content: str) -> list[str]:
    return [next(tok for tok in line.split() if tok.startswith("event=")) for line in content.splitlines()]


def _no_stream(category: str, http_status: int | None = None) -> NoStreamError:
    exc = NoStreamError("extraction failed")
    exc.failure = ExtractionFailure(layer="custom", category=category, http_status=http_status)
    return exc


def _no_iframe(request) -> str:
    return "0" if "/player_load" in request.url else ""


@pytest.mark.unit
class TestNoninteractiveDiagnosticsContext:
    """Runs against a real diagnostics log in the isolated cache directory."""

    def test_episode_and_player_context_precede_each_resolve_result(self, site, make_engine, cli_paths, console, diag_log):
        diagnostics.configure(cli_paths.diagnostics_dir)
        site.series([_EP1])
        site.episode(_EP1, [_PLAYER_A, _PLAYER_B], token=None)
        site.xhr(_no_iframe)

        run(options(), make_engine(), cli_paths)

        content = diag_log()
        assert _events(content) == [
            "event=series_selected",
            "event=episode_selected",
            # the page has players but no XHR token, so the episode is served by api4
            "event=shinden_xhr_context",
            "event=player_selected",
            "event=resolve_result",
            "event=player_selected",
            "event=resolve_result",
        ]
        assert "action=fallback" in content.splitlines()[2]
        assert f"online_id={_PLAYER_A.online_id}" in content.splitlines()[3]
        assert f"online_id={_PLAYER_B.online_id}" in content.splitlines()[5]

    def test_every_source_failing_skips_the_episode_with_a_warning(self, site, make_engine, cli_paths, console, sink, actions):
        site.series([_EP1])
        site.episode(_EP1, [_PLAYER_A, _PLAYER_B], token=None)
        site.xhr(_no_iframe)
        engine = make_engine()

        run(options(), engine, cli_paths)

        assert [e.player for e in sink.of(SourceAttemptFailed)] == ["PlayerA", "PlayerB"]
        assert sink.of(XhrFallback) == [XhrFallback(episode=1.0, reason="auth_missing")]
        assert console.of("warn") == ["Żaden player nie zadziałał dla odcinka 1 — pomijam."]
        actions.play.assert_not_called()
        assert engine.history.watch_entries() == []

    def test_episode_selected_uses_final_episode_number_and_title(self, site, make_engine, cli_paths, console, diag_log):
        diagnostics.configure(cli_paths.diagnostics_dir)
        site.series([_EP1])
        site.episode(_EP1, [])

        run(options(), make_engine(), cli_paths)

        content = diag_log()
        assert f"number={_EP1.number}" in content
        assert _EP1.title in content
        assert "path=/episode/1-fate/view/101" in content
        assert console.of("warn") == ["Brak playerów dla odcinka 1 — pomijam."]


@pytest.mark.unit
class TestMainConfiguresDiagnostics:
    def test_noninteractive_run_records_mode_and_session_end(self, run_main, diag_log, cli_paths, console):
        result = run_main(["--url", SERIES_URL], run=lambda *args: None)

        assert result.code is None
        content = diag_log()
        assert "event=session_start" in content
        assert "mode=noninteractive" in content
        assert "event=session_end" in content
        assert "outcome=ok" in content
        assert any(str(cli_paths.diagnostics_dir) in line for line in console.of("info"))

    def test_run_receives_the_parsed_options_the_engine_and_the_paths(self, run_main, cli_paths):
        seen = []
        result = run_main(["--url", SERIES_URL, "-e", "3"], run=lambda *args: seen.append(args))

        ((opts, engine, paths_arg),) = seen
        assert opts == options(episode="3")
        assert engine is result.engine
        assert paths_arg is cli_paths
        assert isinstance(engine.history, SqliteHistory)
        assert engine.history.path == cli_paths.history_db


@pytest.mark.unit
class TestNoninteractiveHealthDeferAcrossEpisodes:
    """engine.health persists across open_episode calls and defers a known-unavailable host; order never changes."""

    def test_host_marked_unavailable_in_episode_one_is_deferred_in_episode_two(
        self, site, make_engine, cli_paths, console, actions, diag_events, sink
    ):
        site.series([_EP1, _EP2])
        site.episode(_EP1, [player("101", "P1", "pl"), player("102", "P2", "pl"), player("103", "P3", "pl")])
        site.episode(_EP2, [player("104", "P4", "pl"), player("105", "P5", "pl")])
        for online_id, host in (("101", "hostx"), ("102", "hostx"), ("103", "hosty"), ("104", "hostx"), ("105", "hosty")):
            site.embed(online_id, f"https://{host}.example/e/{online_id}")

        def extract(url, _referer, **_kwargs):
            if "hostx" in url:
                raise _no_stream("network_error")
            return make_stream()

        actions.extract.side_effect = extract

        run(options(episode="1-2"), make_engine(), cli_paths)

        # Only 104 (episode 2) is deferred: host X reaches UNAVAILABLE only after 102's failure in episode 1.
        (deferred,) = [fields for event, fields in diag_events if event == "health_defer"]
        assert deferred["action"] == "deferred"
        assert deferred["host"] == "hostx.example"
        assert deferred["online_id"] == "104"
        assert sink.of(HostDeferred) == [HostDeferred(host="hostx.example")]

        # the embed of the deferred candidate is still resolved; only its extraction is skipped
        assert site.shown_ids.count("104") == 1
        assert extracted_urls(actions.extract) == [
            "https://hostx.example/e/101",
            "https://hostx.example/e/102",
            "https://hosty.example/e/103",
            "https://hosty.example/e/105",
        ]
        assert actions.play.call_count == 2


@pytest.mark.unit
class TestNoninteractiveReorder:
    """health.order() tie-breaks unattended candidates within a (lang_audio, lang_subs, max_res) group."""

    def test_no_reorder_event_on_first_episode_without_prior_evidence(self, site, make_engine, cli_paths, console, diag_events):
        site.series([_EP1])
        site.episode(_EP1, [_PLAYER_A, _PLAYER_B])
        site.xhr(_no_iframe)

        run(options(), make_engine(), cli_paths)

        assert site.xhr_urls
        assert not [event for event, _ in diag_events if event == "health_reorder"]

    def test_reorder_from_prior_episode_evidence_respects_group_boundary(
        self, site, make_engine, cli_paths, console, actions, diag_events
    ):
        # Episode 1: A (1080p) soft-fails -> DEGRADED, B (same group) succeeds -> HEALTHY, C (720p) is never tried.
        site.series([_EP1, _EP2])
        site.episode(_EP1, [player("201", "A", res=1080), player("202", "B", res=1080), player("203", "C", res=720)])
        site.episode(_EP2, [player("301", "A", res=1080), player("302", "B", res=1080), player("303", "C", res=720)])
        for online_id, host in (("201", "a"), ("202", "b"), ("203", "c"), ("301", "a"), ("302", "b"), ("303", "c")):
            site.embed(online_id, f"https://{host}-host.example/e/{online_id}")

        def extract(url, _referer, **_kwargs):
            if "a-host" in url and "301" not in url:
                raise _no_stream("http_error", 403)
            return make_stream()

        actions.extract.side_effect = extract

        run(options(episode="1-2"), make_engine(), cli_paths)

        # No reorder in episode 1: no evidence existed yet when it started.
        (reorder,) = [fields for event, fields in diag_events if event == "health_reorder"]
        assert reorder["number"] == _EP2.number
        assert reorder["before"] == "301,302,303"
        # B (healthy) is promoted ahead of A (degraded) within their 1080p group; C (720p) never moves.
        assert reorder["after"] == "302,301,303"
        # states is positionally aligned with after
        assert reorder["states"] == "healthy,degraded,unknown"

        # Episode 1 resolves 201 (fails) then 202 (succeeds); episode 2 starts with B, not A.
        assert site.shown_ids == ["201", "202", "302"]


@pytest.fixture
def history() -> InMemoryHistory:
    return InMemoryHistory()


@pytest.fixture
def engine(site, make_engine, history, actions, console) -> Engine:
    """Five episodes with one working player each; only target selection and history writes vary per test."""
    site.series(_EPISODES)
    for ep in _EPISODES:
        site.episode(ep, [_PLAYER_A])
    return make_engine(history=history)


def _last_watched(history: InMemoryHistory) -> list[float]:
    return [entry.last_ep for entry in history.watch_entries()]


def _downloaded(history: InMemoryHistory) -> list[frozenset[float]]:
    return [entry.episodes for entry in history.download_entries()]


@pytest.mark.unit
class TestNoninteractiveDownloadHistory:
    def test_continue_without_download_uses_watch_history(self, engine, history, actions, cli_paths):
        history.record_watch(REF, 2.0)
        history.record_download(REF, 1.0)

        run(options(url=None, resume=True), engine, cli_paths)

        assert "Odcinek 3" in actions.play.call_args.kwargs["title"]
        actions.download.assert_not_called()
        assert _last_watched(history) == [3.0]
        assert _downloaded(history) == [frozenset({1.0})]

    def test_continue_with_download_uses_download_history(self, engine, history, actions, cli_paths):
        history.record_watch(REF, 4.0)
        history.record_download(REF, 1.0)
        history.record_download(REF, 2.0)

        run(options(url=None, resume=True, download=True), engine, cli_paths)

        variant, ep, ref, dest = actions.download.call_args.args
        assert ep.number == 3.0
        assert variant is actions.extract.return_value.default
        assert ref.id == REF.id
        assert dest == cli_paths.downloads_dir
        assert _downloaded(history) == [frozenset({1.0, 2.0, 3.0})]
        assert _last_watched(history) == [4.0]
        actions.play.assert_not_called()

    def test_continue_with_download_picks_first_gap(self, engine, history, actions, cli_paths):
        for number in (1.0, 2.0, 4.0):
            history.record_download(REF, number)

        run(options(url=None, resume=True, download=True), engine, cli_paths)

        assert actions.download.call_args.args[1].number == 3.0

    def test_continue_with_download_all_downloaded_skips_download(self, engine, history, actions, cli_paths, console, site):
        for ep in _EPISODES:
            history.record_download(REF, ep.number)

        run(options(url=None, resume=True, download=True), engine, cli_paths)

        actions.download.assert_not_called()
        assert _downloaded(history) == [frozenset(ep.number for ep in _EPISODES)]
        assert console.of("info")[-1] == f"Wszystkie dostępne odcinki są już pobrane ({REF.title})."
        assert site.episode_urls == []

    def test_continue_with_download_empty_history_exits(self, engine, history, actions, cli_paths, console, transport):
        history.record_watch(REF, 2.0)

        with pytest.raises(SystemExit) as exc_info:
            run(options(url=None, resume=True, download=True), engine, cli_paths)

        assert exc_info.value.code == 1
        assert console.of("error") == ["Historia pobierania jest pusta."]
        actions.download.assert_not_called()
        assert transport.requests == []

    def test_continue_with_empty_watch_history_exits(self, engine, actions, cli_paths, console, transport):
        with pytest.raises(SystemExit) as exc_info:
            run(options(url=None, resume=True), engine, cli_paths)

        assert exc_info.value.code == 1
        assert console.of("error") == ["Historia jest pusta."]
        actions.play.assert_not_called()
        assert transport.requests == []

    def test_continue_after_the_last_episode_warns_and_starts_over(self, engine, history, actions, cli_paths, console):
        history.record_watch(REF, 5.0)

        run(options(url=None, resume=True), engine, cli_paths)

        assert console.of("warn") == [f"Obejrzałeś już wszystkie odcinki ({REF.title})."]
        assert "Odcinek 1" in actions.play.call_args.kwargs["title"]
        assert _last_watched(history) == [1.0]

    def test_plain_download_records_download_history(self, engine, history, actions, cli_paths):
        run(options(download=True, episode="2"), engine, cli_paths)

        assert _downloaded(history) == [frozenset({2.0})]
        assert _last_watched(history) == []

    def test_failed_download_does_not_record_history(self, engine, history, actions, cli_paths):
        actions.download.side_effect = DownloadFailedError("x")

        with pytest.raises(DownloadFailedError):
            run(options(download=True, episode="2"), engine, cli_paths)

        assert _downloaded(history) == []
        assert _last_watched(history) == []

    def test_download_never_offers_overwrite(self, engine, history, actions, cli_paths):
        with patch("alt_ani_cli.ui.menus.select_existing_download_action") as mock_prompt:
            run(options(download=True, episode="2"), engine, cli_paths)

        assert actions.download.call_args.kwargs == {"events": engine.events}
        mock_prompt.assert_not_called()
        assert _downloaded(history) == [frozenset({2.0})]

    def test_download_ranks_original_audio_first(self, site, engine, actions, cli_paths):
        site.episode(_EP1, [player("31", "Dub", audio="pl"), player("32", "Original", audio="jp")])

        run(options(download=True), engine, cli_paths)
        assert site.shown_ids == ["32"]

        run(options(), engine, cli_paths)
        assert site.shown_ids == ["32", "31"]


@pytest.mark.unit
class TestNoninteractiveWatch:
    def test_default_plays_the_first_episode_and_records_it(self, engine, history, actions, cli_paths, console):
        run(options(), engine, cli_paths)

        variant = actions.play.call_args.args[0]
        assert variant is actions.extract.return_value.default
        assert actions.play.call_args.kwargs == {"kind": "mpv", "title": "Fate — Odcinek 1", "no_detach": False}
        assert console.of("success") == ["Odtwarzam w mpv: Fate — Odcinek 1"]
        assert _last_watched(history) == [1.0]

    def test_range_plays_every_episode_in_order(self, engine, history, actions, cli_paths):
        run(options(episode="2-4"), engine, cli_paths)

        titles = [call.kwargs["title"] for call in actions.play.call_args_list]
        assert titles == ["Fate — Odcinek 2", "Fate — Odcinek 3", "Fate — Odcinek 4"]
        assert _last_watched(history) == [4.0]

    def test_vlc_and_no_detach_reach_the_player(self, engine, actions, cli_paths):
        run(options(vlc=True, no_detach=True), engine, cli_paths)

        assert actions.play.call_args.kwargs["kind"] == "vlc"
        assert actions.play.call_args.kwargs["no_detach"] is True

    def test_unconfirmed_attached_playback_is_not_recorded(self, engine, history, actions, cli_paths, console):
        actions.play.return_value = MagicMock(rc=0, elapsed=0.5)

        run(options(no_detach=True), engine, cli_paths)

        assert _last_watched(history) == []
        assert console.of("success") == []
        assert len(console.of("warn")) == 1

    def test_best_quality_is_the_default(self, engine, actions, cli_paths):
        actions.extract.return_value = make_stream(
            "https://cdn.example/480.mp4",
            qualities={"480p": "https://cdn.example/480.mp4", "1080p": "https://cdn.example/1080.mp4"},
        )

        run(options(), engine, cli_paths)

        assert actions.play.call_args.args[0].url == "https://cdn.example/1080.mp4"

    def test_requested_quality_selects_that_variant(self, engine, actions, cli_paths):
        actions.extract.return_value = make_stream(
            "https://cdn.example/480.mp4",
            qualities={"480p": "https://cdn.example/480.mp4", "1080p": "https://cdn.example/1080.mp4"},
        )

        run(options(quality="480p"), engine, cli_paths)

        assert actions.play.call_args.args[0].url == "https://cdn.example/480.mp4"

    def test_cookie_options_reach_the_extractor(self, engine, actions, cli_paths):
        run(options(cookies_file="c.txt", cookies_browser="firefox"), engine, cli_paths)

        assert actions.extract.call_args.kwargs["options"] == ExtractOptions(cookies_file="c.txt", cookies_browser="firefox")

    def test_debug_prints_the_stream_without_playing_or_recording(self, engine, history, actions, cli_paths):
        with patch("alt_ani_cli.playback.print_debug") as mock_debug:
            run(options(debug=True), engine, cli_paths)

        stream, variant, embed = mock_debug.call_args.args
        assert stream is actions.extract.return_value
        assert variant is stream.default
        assert embed.url == "https://playera.example/e/11"
        actions.play.assert_not_called()
        actions.download.assert_not_called()
        assert _last_watched(history) == []

    def test_title_comes_from_the_episode_list_page(self, site, engine, actions, cli_paths, console):
        site.series(_EPISODES, title="Fate/Zero")

        run(options(), engine, cli_paths)

        assert actions.play.call_args.kwargs["title"] == "Fate/Zero — Odcinek 1"
        assert engine.history.watch_entries()[0].series.title == "Fate/Zero"

    def test_empty_episode_list_exits(self, site, engine, actions, cli_paths, console):
        site.series([])

        with pytest.raises(SystemExit) as exc_info:
            run(options(), engine, cli_paths)

        assert exc_info.value.code == 1
        assert console.of("error") == ["Brak dostępnych odcinków."]


@pytest.mark.unit
class TestNoninteractiveInput:
    def test_missing_input_is_a_usage_error(self, engine, cli_paths, transport, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run(options(url=None), engine, cli_paths)

        assert exc_info.value.code == 2
        assert "--url" in capsys.readouterr().err
        assert transport.requests == []

    def test_search_without_results_exits(self, cli_paths, console):
        engine = MagicMock(spec=Engine)
        engine.search.return_value = []

        with pytest.raises(SystemExit) as exc_info:
            run(options(url=None, query=("fate", "zero")), engine, cli_paths)

        assert exc_info.value.code == 1
        engine.search.assert_called_once_with("fate zero")
        assert console.of("error") == ["Brak wyników dla: 'fate zero'"]
        engine.episodes.assert_not_called()

    def test_select_nth_beyond_the_results_exits(self, cli_paths, console):
        engine = MagicMock(spec=Engine)
        engine.search.return_value = [SeriesHit(id="1", slug="fate", title="Fate", url=SERIES_URL)]

        with pytest.raises(SystemExit) as exc_info:
            run(options(url=None, query=("fate",), select_nth=2), engine, cli_paths)

        assert exc_info.value.code == 1
        assert console.of("error") == ["--select-nth 2: jest tylko 1 wyników."]
        engine.episodes.assert_not_called()

    def test_select_nth_picks_that_hit_and_keeps_its_title(self, engine, actions, cli_paths):
        hits = [
            SeriesHit(id="9", slug="other", title="Other", url="https://shinden.pl/series/9-other"),
            SeriesHit(id="1", slug="fate", title="Fate (TV)", url=SERIES_URL),
        ]
        with (
            patch.object(engine, "search", return_value=hits) as mock_search,
            patch.object(engine, "episodes", wraps=engine.episodes) as mock_episodes,
        ):
            run(options(url=None, query=("fate",), select_nth=2), engine, cli_paths)

        mock_search.assert_called_once_with("fate")
        ref = mock_episodes.call_args.args[0]
        assert (ref.id, ref.title, ref.url) == ("1", "Fate (TV)", SERIES_URL)
        actions.play.assert_called_once()


@pytest.mark.unit
class TestNoninteractiveEpisodeRange:
    def test_unrecognised_range_warns_then_reports_no_match_and_exits(self, engine, actions, cli_paths, console, site):
        with pytest.raises(SystemExit) as exc_info:
            run(options(episode="abc"), engine, cli_paths)

        assert exc_info.value.code == 1
        assert [m for m in console.messages if m[0] != "info"] == [
            ("warn", "Nie rozpoznano formatu zakresu odcinków: 'abc' — ignoruję."),
            ("error", "Nie znaleziono odcinków dla zakresu: 'abc'"),
        ]
        assert site.episode_urls == []
        actions.play.assert_not_called()

    def test_recognised_range_without_match_exits_without_the_format_warning(self, engine, actions, cli_paths, console):
        with pytest.raises(SystemExit) as exc_info:
            run(options(episode="99"), engine, cli_paths)

        assert exc_info.value.code == 1
        assert console.of("warn") == []
        assert console.of("error") == ["Nie znaleziono odcinków dla zakresu: '99'"]


@pytest.mark.unit
class TestNoninteractiveFilters:
    @pytest.fixture(autouse=True)
    def _two_players(self, site, engine):
        site.episode(_EP1, [_PLAYER_A, _PLAYER_B])

    def test_mismatch_raises_before_any_xhr(self, site, engine, actions, cli_paths):
        with pytest.raises(FilterMismatchError, match="--lang=xx"):
            run(options(lang="xx"), engine, cli_paths)

        assert site.xhr_urls == []
        actions.play.assert_not_called()
        assert engine.history.watch_entries() == []

    def test_allow_fallback_warns_and_uses_the_full_list(self, site, engine, actions, cli_paths, console):
        run(options(lang="xx", allow_fallback=True), engine, cli_paths)

        assert console.of("warn") == ["Filtry (--lang=xx) nie pasują do żadnego playera — używam pełnej listy."]
        assert site.shown_ids == ["11"]
        actions.play.assert_called_once()

    def test_matching_filter_narrows_the_candidates(self, site, engine, actions, cli_paths, console):
        actions.extract.side_effect = _no_stream("no_stream_url")

        run(options(player_name="playerb"), engine, cli_paths)

        assert site.shown_ids == ["22"]
        assert console.of("warn")[-1] == "Żaden player nie zadziałał dla odcinka 1 — pomijam."

    def test_main_reports_a_mismatch_and_exits_1(self, site, engine, actions, run_main, console):
        result = run_main(["--url", SERIES_URL, "--lang", "xx"])

        assert result.code == 1
        assert "--lang=xx" in console.of("error")[-1]
        assert result.engine.closed


def _thirty_of_forty_pages(request) -> str:
    return episodes_html([episode(int(request.params["strona"]))], last_page=40)


_RESUME_CASES = [
    {},
    {"episode": "1-5"},
    {"episode": "3"},
    {"episode": "1 2"},
    {"episode": "-1"},
    {"url": None, "resume": True},
    {"url": None, "resume": True, "download": True},
    {"download": True},
]


@pytest.mark.unit
class TestNoninteractiveIncompleteEpisodeList:
    """Every unattended selection treats the list as the full catalogue, so a partial one must abort."""

    @pytest.fixture(autouse=True)
    def _seeded_history(self, engine, history):
        history.record_watch(REF, 2.0)
        history.record_download(REF, 1.0)

    def _assert_nothing_happened(self, site, history, actions):
        assert site.episode_urls == []
        assert site.xhr_urls == []
        actions.play.assert_not_called()
        actions.download.assert_not_called()
        assert _last_watched(history) == [2.0]
        assert _downloaded(history) == [frozenset({1.0})]

    @pytest.mark.parametrize(
        "overrides",
        _RESUME_CASES,
        ids=["default", "range", "single", "multi", "last", "continue", "continue-download", "download"],
    )
    def test_partial_listing_aborts_before_any_selection(self, overrides, site, transport, engine, history, actions, cli_paths):
        transport.route(f"{SERIES_URL}/episodes", _thirty_of_forty_pages)
        with (
            patch("alt_ani_core.cancel.CancelToken.wait"),
            pytest.raises(IncompleteEpisodeListError, match=r"30/40 pages, page_limit"),
        ):
            run(options(**overrides), engine, cli_paths)

        self._assert_nothing_happened(site, history, actions)

    @pytest.mark.parametrize("overrides", [{}, {"episode": "1"}, {"url": None, "resume": True}, {"download": True}], ids=str)
    def test_counter_mismatch_aborts_before_play_download_and_history(
        self, overrides, site, engine, history, actions, cli_paths
    ):
        site.series(_EPISODES, count=9)

        with pytest.raises(IncompleteEpisodeListError, match="count_mismatch"):
            run(options(**overrides), engine, cli_paths)

        self._assert_nothing_happened(site, history, actions)

    def test_complete_listing_still_plays(self, engine, actions, cli_paths):
        run(options(), engine, cli_paths)

        actions.play.assert_called_once()

    def test_main_reports_error_and_exits_1(self, site, engine, actions, run_main, console):
        site.series(_EPISODES, count=9)

        result = run_main(["--url", SERIES_URL])

        assert result.code == 1
        assert "Episode list for Fate is incomplete (1/1 pages, count_mismatch)" in console.of("error")[-1]
        actions.play.assert_not_called()
        assert result.engine.closed


_SECRET_COOKIE = "cf_clearance=SECRETCOOKIE123"
_SECRET_MESSAGE = "session token SECRETTOKEN456"
_XHR_ARGV = ["--url", SERIES_URL, "-e", "1-2"]


@pytest.fixture
def xhr_site(site, actions):
    """Two episodes on the api4 fallback (no page token), with the anti-bot wait observable."""
    site.series([_EP1, _EP2])
    site.episode(_EP1, [_PLAYER_A, _PLAYER_B], token=None)
    site.episode(_EP2, [_PLAYER_A, _PLAYER_B], token=None)
    with patch("alt_ani_core.cancel.CancelToken.wait") as mock_wait:
        site.wait = mock_wait
        yield site


def _watch_history(cli_paths) -> list:
    return SqliteHistory(cli_paths.history_db).watch_entries()


@pytest.mark.unit
class TestNoninteractiveXhrStop:
    def test_access_block_exits_1_without_further_xhr_episodes_or_history(
        self, xhr_site, run_main, console, actions, transport, cli_paths, diag_log
    ):
        body = json.dumps({"error": {"code": "ochrona_godz", "message": _SECRET_MESSAGE, "retry_after": 300}})
        xhr_site.xhr(FakeResponse(body, 429, {"Retry-After": "120", "Set-Cookie": _SECRET_COOKIE}))

        result = run_main(_XHR_ARGV)

        assert result.code == 1
        assert len(xhr_site.xhr_urls) == 1
        assert xhr_site.xhr_urls[0].startswith(SHINDEN_API_BASE)
        assert _EP2.url not in transport.urls
        xhr_site.wait.assert_not_called()
        assert _watch_history(cli_paths) == []
        actions.play.assert_not_called()

        (error,) = console.of("error")
        assert "blocked" in error
        assert "120 s" in error
        assert "ochrona_godz" in error

        log = diag_log()
        assert "layer=shinden_api" in log
        assert "stage=player_load" in log
        assert "http_status=429" in log
        assert "api_code=ochrona_godz" in log
        assert "retry_after=120" in log
        assert "xhr_outcome=access_blocked" in log
        for secret in ("SECRETCOOKIE123", "SECRETTOKEN456", GUEST_AUTH, "auth="):
            assert secret not in log
            assert secret not in console.text
        assert result.engine.closed
        assert all(session.closed for session in transport.sessions)

    def test_access_block_on_the_new_backend_never_shows_the_page_token(self, xhr_site, run_main, console, transport, diag_log):
        xhr_site.episode(_EP1, [_PLAYER_A, _PLAYER_B])
        xhr_site.xhr(FakeResponse(json.dumps({"error": {"code": "turnstile"}}), 403))

        result = run_main(_XHR_ARGV)

        assert result.code == 1
        (xhr_url,) = xhr_site.xhr_urls
        assert xhr_url.startswith(SHINDEN_XHR_BASE)
        assert quote(TEST_TOKEN, safe="") in xhr_url
        assert _EP2.url not in transport.urls
        assert "blocked" in console.of("error")[-1]
        for secret in (TEST_TOKEN, quote(TEST_TOKEN, safe=""), "auth="):
            assert secret not in diag_log()
            assert secret not in console.text

    def test_503_exits_1_as_unavailability_not_antibot(self, xhr_site, run_main, console):
        xhr_site.xhr(FakeResponse("<html>Service Unavailable</html>", 503))

        result = run_main(_XHR_ARGV)

        assert result.code == 1
        assert len(xhr_site.xhr_urls) == 1
        assert "unavailable" in console.text
        assert "antibot" not in console.text.lower()
        assert "blocked" not in console.text

    def test_invalid_wait_stops_with_protocol_error_and_reason_in_diagnostics(self, xhr_site, run_main, console, diag_log):
        xhr_site.xhr("45")

        result = run_main(_XHR_ARGV)

        assert result.code == 1
        assert len(xhr_site.xhr_urls) == 1
        xhr_site.wait.assert_not_called()
        log = diag_log()
        assert "category=parser_drift" in log
        assert "reason=wait_above_limit" in log
        assert "xhr_outcome=api_protocol_error" in log

    def test_dns_failure_on_xhr_exits_1_without_traceback(self, xhr_site, run_main, console, capsys):
        xhr_site.xhr(NetworkError("could not resolve api4.shinden.pl"))

        result = run_main(_XHR_ARGV)

        assert result.code == 1
        assert len(xhr_site.xhr_urls) == 1
        assert "Traceback" not in capsys.readouterr().err
        assert "network_error" in console.of("error")[-1]

    def test_source_specific_failure_walks_on_to_the_next_player(self, xhr_site, run_main, console, actions):
        xhr_site.xhr(lambda request: FakeResponse("", 404) if "/11/" in request.url else xhr_site._xhr_ok(request))

        result = run_main(_XHR_ARGV)

        assert result.code is None
        assert xhr_site.shown_ids == ["22", "22"]
        assert actions.play.call_count == 2


@pytest.mark.unit
class TestMainTransportErrorOutsideXhr:
    def test_episode_page_timeout_exits_1_without_traceback(self, site, transport, run_main, console, capsys, actions):
        site.series([_EP1])
        transport.route(_EP1.url, TransportTimeout("timed out: https://shinden.pl/ep/1?token=SECRET"))

        result = run_main(["--url", SERIES_URL])

        assert result.code == 1
        assert "Traceback" not in capsys.readouterr().err
        (error,) = console.of("error")
        assert error.startswith("Błąd sieci:")
        assert "token=SECRET" not in console.text
        assert result.engine.closed
        assert all(session.closed for session in transport.sessions)


@pytest.mark.unit
class TestMainEndToEnd:
    def test_watch_run_records_history_in_sqlite_and_closes_the_engine(
        self, site, engine, actions, run_main, console, cli_paths, transport, diag_log
    ):
        result = run_main(["--url", SERIES_URL, "-e", "2"])

        assert result.code is None
        assert actions.play.call_args.kwargs["title"] == "Fate — Odcinek 2"
        (entry,) = _watch_history(cli_paths)
        assert (entry.series.id, entry.last_ep) == ("1", 2.0)
        assert "outcome=ok" in diag_log()
        assert result.engine.closed
        assert transport.sessions
        assert all(session.closed for session in transport.sessions)

    def test_download_run_records_history_and_renders_engine_events(self, site, engine, actions, run_main, console, cli_paths):
        result = run_main(["--url", SERIES_URL, "-d", "-e", "3"])

        assert result.code is None
        assert actions.download.call_args.args[3] == cli_paths.downloads_dir
        (entry,) = SqliteHistory(cli_paths.history_db).download_entries()
        assert entry.episodes == frozenset({3.0})
        assert _watch_history(cli_paths) == []
        assert "Embed: https://playera.example/e/11" in console.of("info")

    def test_continue_resumes_from_sqlite_history(self, site, engine, actions, run_main, cli_paths):
        SqliteHistory(cli_paths.history_db).record_watch(REF, 3.0)

        result = run_main(["-c"])

        assert result.code is None
        assert actions.play.call_args.kwargs["title"] == "Fate — Odcinek 4"
        assert [entry.last_ep for entry in _watch_history(cli_paths)] == [4.0]

    def test_continue_download_resumes_from_sqlite_history(self, site, engine, actions, run_main, cli_paths):
        store = SqliteHistory(cli_paths.history_db)
        store.record_download(REF, 1.0)
        store.record_download(REF, 2.0)

        result = run_main(["-c", "-d"])

        assert result.code is None
        assert actions.download.call_args.args[1].number == 3.0
        assert [entry.episodes for entry in store.download_entries()] == [frozenset({1.0, 2.0, 3.0})]
        actions.play.assert_not_called()

    def test_debug_run_prints_the_stream_and_records_nothing(self, site, engine, actions, run_main, cli_paths):
        with patch("alt_ani_cli.playback.print_debug") as mock_debug:
            result = run_main(["--url", SERIES_URL, "--debug"])

        assert result.code is None
        mock_debug.assert_called_once()
        actions.play.assert_not_called()
        assert _watch_history(cli_paths) == []
