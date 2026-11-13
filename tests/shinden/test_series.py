"""Tests for shinden/series.py against sanitised Shinden 2.5 episode-list responses."""

import re
from unittest.mock import MagicMock

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import SHINDEN_BASE
from alt_ani_core.errors import OperationCancelled, ParseError, ShindenError
from alt_ani_core.models import SeriesRef
from alt_ani_core.shinden import series
from alt_ani_core.shinden.series import MAX_EPISODE_PAGES, _parse_page, list_episodes, parse_series_url
from alt_ani_core.shinden.utils import ParseTally
from alt_ani_core.testing import RecordingCancel
from alt_ani_core.transport.api import HttpStatusError, NetworkError

_FRIEREN = SeriesRef(
    id="61064", slug="sousou-no-frieren", title="Sousou No Frieren", url=f"{SHINDEN_BASE}/series/61064-sousou-no-frieren"
)
_ONE_PIECE = SeriesRef(id="12", slug="one-piece", title="One Piece", url=f"{SHINDEN_BASE}/titles/12-one-piece")

_UNAVAILABLE_ROW_RE = re.compile(
    r'<span class="ep-row__link">(\s*<span class="ep-row__no">(\d+)</span>\s*<span class="ep-row__title">[^<]*</span>'
    r'(?:\s*<span class="ep-row__badge[^"]*">[^<]*</span>)?\s*)</span>'
)


def _with_links(html: str) -> str:
    """Turn the real unavailable rows of a /titles/ page into playable rows, keeping the page layout intact."""
    return _UNAVAILABLE_ROW_RE.sub(r'<a class="ep-row__link" href="/episode/12-one-piece/view/\2">\1</a>', html)


def _paged(html: str, total: int, last_page: int) -> str:
    html = re.sub(r'ep-list__count">\d+', f'ep-list__count">{total}', html)
    return re.sub(
        r"strona=\d+&amp;na_stronie", lambda m: f"strona={min(int(m.group(0)[7:-15]), last_page)}&amp;na_stronie", html
    )


def _shifted(html: str, offset: int) -> str:
    """Give a page distinct episode URLs and numbers so it is not mistaken for a repeated page."""
    html = re.sub(r'view/(\d+)"', lambda m: f'view/{int(m.group(1)) + offset}"', html)
    return re.sub(r'ep-row__no">(\d+)<', lambda m: f'ep-row__no">{int(m.group(1)) + offset}<', html)


def _truncated(html: str, keep: int) -> str:
    """Keep only the first ``keep`` rows of the episode list; the related-titles section after it is untouched."""
    rows = iter(range(series.EPISODES_PER_PAGE))
    return re.sub(r'<li class="ep-row".*?</li>', lambda m: m.group(0) if next(rows, -1) < keep else "", html, flags=re.S)


def _events(events, name):
    return [fields for event, fields in events if event == name]


@pytest.fixture(autouse=True)
def _no_page_delay(monkeypatch):
    monkeypatch.setattr(CancelToken, "wait", lambda _self, _seconds: None)


@pytest.fixture
def client_for(fake_response):
    """Client whose GET answers by page number; a callable value may raise or build the response."""

    def _make(pages: dict, default=None):
        def _get(url, params=None):
            page = (params or {}).get("strona", 1)
            body = pages.get(page, default)
            if callable(body):
                return body(url)
            return fake_response(body)

        client = MagicMock()
        client.get.side_effect = _get
        return client

    return _make


@pytest.mark.unit
class TestParseSeriesUrl:
    def test_series_route(self):
        ref = parse_series_url("https://shinden.pl/series/61064-sousou-no-frieren/episodes")
        assert (ref.id, ref.slug) == ("61064", "sousou-no-frieren")
        assert ref.url == f"{SHINDEN_BASE}/series/61064-sousou-no-frieren"

    def test_titles_route_keeps_its_kind(self):
        ref = parse_series_url("https://shinden.pl/titles/12-one-piece")
        assert ref.url == f"{SHINDEN_BASE}/titles/12-one-piece"

    def test_without_suffix(self):
        assert parse_series_url("https://shinden.pl/series/65137-fate-strange-fake").id == "65137"

    def test_invalid_raises_parse_error(self):
        with pytest.raises(ParseError):
            parse_series_url("https://shinden.pl/manga/57765-sousou-no-frieren")


@pytest.mark.unit
class TestParsePage:
    def test_frieren_page(self, shinden_html):
        tally = ParseTally("episodes")
        page = _parse_page(shinden_html("episodes_frieren.html"), tally)
        assert page.title == "Sousou no Frieren"
        assert (len(page.rows), len(page.row_keys), page.total, page.last_page) == (28, 28, 28, 1)
        assert page.rows[0].url == f"{SHINDEN_BASE}/episode/61064-sousou-no-frieren/view/227851"
        assert page.rows[0].title == "To byłoby zawstydzające, gdybyśmy spotkali się ponownie"

    def test_titles_page_rows_are_excluded_as_unavailable(self, shinden_html):
        tally = ParseTally("episodes")
        page = _parse_page(shinden_html("episodes_titles_p1.html"), tally)
        assert page.rows == []
        assert (page.total, page.last_page) == (1181, 12)
        assert tally.excluded == {"unavailable": 100}
        assert not tally.invalid

    def test_related_titles_section_is_ignored(self, shinden_html):
        tally = ParseTally("episodes")
        page = _parse_page(_with_links(shinden_html("episodes_titles_p1.html")), tally)
        assert len(page.row_keys) == 100
        assert min(ep.number for ep in page.rows) == 1082

    def test_empty_series(self, shinden_html):
        page = _parse_page(shinden_html("episodes_empty.html"), ParseTally("episodes"))
        assert (page.rows, page.total) == ([], 0)

    def test_missing_section_raises(self, shinden_html):
        html = shinden_html("episodes_frieren.html").replace('id="odcinki"', 'id="renamed"')
        with pytest.raises(ParseError, match="section#odcinki"):
            _parse_page(html, ParseTally("episodes"))

    def test_counter_without_rows_raises(self, shinden_html):
        html = shinden_html("episodes_frieren.html").replace('class="ep-row has-groups"', 'class="ep-renamed"')
        with pytest.raises(ParseError, match="28 episodes"):
            _parse_page(html, ParseTally("episodes"))

    @pytest.mark.parametrize(
        "counter",
        [
            pytest.param("", id="missing"),
            pytest.param('<span class="ep-list__count">?</span>', id="unrecognised"),
            pytest.param('<span class="ep-list__count">-1</span>', id="negative"),
        ],
    )
    def test_empty_list_without_zero_counter_raises(self, shinden_html, counter):
        html = shinden_html("episodes_empty.html").replace('<span class="ep-list__count">0</span>', counter)
        with pytest.raises(ParseError, match="counter missing or unrecognised"):
            _parse_page(html, ParseTally("episodes"))

    def test_rows_without_counter_are_still_parsed(self, shinden_html):
        html = shinden_html("episodes_frieren.html").replace('<span class="ep-list__count">28</span>', "")
        page = _parse_page(html, ParseTally("episodes"))
        assert (len(page.rows), page.total) == (28, None)

    @pytest.mark.parametrize("raw, expected", [("12.5", 12.5), ("12,5", 12.5), (" 7 ", 7.0)])
    def test_non_integer_numbers(self, shinden_html, raw, expected):
        html = shinden_html("episodes_frieren.html").replace('ep-row__no">28<', f'ep-row__no">{raw}<')
        page = _parse_page(html, ParseTally("episodes"))
        assert page.rows[0].number == expected

    @pytest.mark.parametrize("raw", ["SP", "", "-1", "nan", "inf"])
    def test_bad_number_is_invalid(self, shinden_html, raw):
        tally = ParseTally("episodes")
        html = shinden_html("episodes_frieren.html").replace('ep-row__no">28<', f'ep-row__no">{raw}<')
        page = _parse_page(html, tally)
        assert len(page.rows) == 27
        assert tally.invalid == {"bad_number": 1}

    def test_row_without_any_link_is_invalid(self, shinden_html):
        tally = ParseTally("episodes")
        html = shinden_html("episodes_frieren.html").replace('<a class="ep-row__link"', '<a class="ep-renamed"', 1)
        _parse_page(html, tally)
        assert tally.invalid == {"no_link": 1}

    def test_link_outside_episode_route_is_invalid(self, shinden_html):
        tally = ParseTally("episodes")
        html = shinden_html("episodes_frieren.html").replace('href="/episode/61064', 'href="/grupa/61064', 1)
        _parse_page(html, tally)
        assert tally.invalid == {"bad_href": 1}

    def test_title_falls_back_to_title_tag_without_h1(self, shinden_html):
        html = re.sub(r"<h1[^>]*>.*?</h1>", "", shinden_html("episodes_frieren.html"), flags=re.S)
        assert _parse_page(html, ParseTally("episodes")).title == "Sousou no Frieren (anime)"

    def test_challenge_page_raises(self):
        with pytest.raises(ParseError, match="Cloudflare challenge"):
            _parse_page("<html><head><title>Just a moment...</title></head></html>", ParseTally("episodes"))

    def test_age_gate_raises(self):
        with pytest.raises(ShindenError):
            _parse_page("<html><body>Musisz mieć ukończone 18 lat</body></html>", ParseTally("episodes"))


@pytest.mark.unit
class TestListEpisodes:
    def test_single_page_ascending_and_complete(self, shinden_html, client_for, diag_events):
        client = client_for({1: shinden_html("episodes_frieren.html")})
        listing = list_episodes(client, _FRIEREN)
        assert listing.complete
        assert [ep.number for ep in listing.episodes] == [float(n) for n in range(1, 29)]
        assert listing.ref.title == "Sousou no Frieren"
        client.get.assert_called_once_with(f"{_FRIEREN.url}/episodes", params={"strona": 1, "na_stronie": 100})
        assert diag_events == []

    def test_empty_series_is_empty_and_complete(self, shinden_html, client_for):
        listing = list_episodes(client_for({1: shinden_html("episodes_empty.html")}), _FRIEREN)
        assert (listing.episodes, listing.complete) == ([], True)

    def test_titles_series_returns_no_episodes_without_error(self, shinden_html, client_for, diag_events):
        p1 = _paged(shinden_html("episodes_titles_p1.html"), total=200, last_page=2)
        p2 = _paged(shinden_html("episodes_titles_p2.html"), total=200, last_page=2)
        listing = list_episodes(client_for({1: p1, 2: p2}), _ONE_PIECE)
        assert (listing.episodes, listing.complete) == ([], True)
        assert _events(diag_events, "shinden_parse_partial") == []

    def test_pages_merged_in_order(self, shinden_html, client_for):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        p2 = _paged(_with_links(shinden_html("episodes_titles_p2.html")), total=200, last_page=2)
        client = client_for({1: p1, 2: p2})
        listing = list_episodes(client, _ONE_PIECE)
        assert listing.complete
        assert (listing.pages_fetched, listing.pages_total) == (2, 2)
        numbers = [ep.number for ep in listing.episodes]
        assert numbers == sorted(numbers)
        assert (numbers[0], numbers[-1], len(numbers)) == (982.0, 1181.0, 200)
        assert [c.kwargs["params"]["strona"] for c in client.get.call_args_list] == [1, 2]

    def test_repeated_page_is_incomplete_even_when_row_sum_matches_counter(self, shinden_html, client_for, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        listing = list_episodes(client_for({1: p1, 2: p1}), _ONE_PIECE)
        assert (listing.complete, listing.incomplete_reason) == (False, "repeated_page")
        assert len(listing.episodes) == 100
        assert _events(diag_events, "shinden_episodes_incomplete") == [
            {"id": "12", "fetched": 2, "total": 2, "reason": "repeated_page"}
        ]
        assert _events(diag_events, "shinden_parse_partial") == []

    def test_repeated_page_stops_fetching(self, shinden_html, client_for):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=300, last_page=3)
        client = client_for({1: p1}, default=p1)
        assert list_episodes(client, _ONE_PIECE).incomplete_reason == "repeated_page"
        assert client.get.call_count == 2

    def test_repeated_page_of_unavailable_rows_is_detected(self, shinden_html, client_for):
        p1 = _paged(shinden_html("episodes_titles_p1.html"), total=200, last_page=2)
        listing = list_episodes(client_for({1: p1, 2: p1}), _ONE_PIECE)
        assert (listing.episodes, listing.incomplete_reason) == ([], "repeated_page")

    def test_single_repeated_row_is_tolerated_and_logged(self, shinden_html, client_for, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        p2 = _paged(_with_links(shinden_html("episodes_titles_p2.html")), total=200, last_page=2)
        listing = list_episodes(client_for({1: p1, 2: p2.replace('view/1081"', 'view/1082"')}), _ONE_PIECE)
        assert listing.complete
        assert len(listing.episodes) == 199
        assert {"page": "episodes", "counter": "page_overlap", "expected": 0, "actual": 1} in _events(
            diag_events, "shinden_count_mismatch"
        )

    def test_two_repeated_rows_mark_listing_incomplete(self, shinden_html, client_for):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        p2 = _paged(_with_links(shinden_html("episodes_titles_p2.html")), total=200, last_page=2)
        p2 = p2.replace('view/1081"', 'view/1082"').replace('view/1080"', 'view/1083"')
        assert list_episodes(client_for({1: p1, 2: p2}), _ONE_PIECE).incomplete_reason == "repeated_page"

    def test_empty_later_page_is_incomplete(self, shinden_html, client_for, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        p2 = _truncated(_paged(shinden_html("episodes_titles_p2.html"), total=0, last_page=2), keep=0)
        listing = list_episodes(client_for({1: p1, 2: p2}), _ONE_PIECE)
        assert (listing.complete, listing.incomplete_reason) == (False, "empty_page")
        assert len(listing.episodes) == 100
        assert _events(diag_events, "shinden_episodes_incomplete") == [
            {"id": "12", "fetched": 2, "total": 2, "reason": "empty_page"}
        ]

    def test_empty_page_stops_fetching(self, shinden_html, client_for):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=300, last_page=3)
        empty = _truncated(_paged(shinden_html("episodes_titles_p2.html"), total=0, last_page=3), keep=0)
        client = client_for({1: p1}, default=empty)
        assert list_episodes(client, _ONE_PIECE).incomplete_reason == "empty_page"
        assert client.get.call_count == 2

    def test_empty_last_page_after_one_removed_episode_is_tolerated(self, shinden_html, client_for, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=101, last_page=2)
        p2 = _truncated(_paged(shinden_html("episodes_titles_p2.html"), total=0, last_page=2), keep=0)
        listing = list_episodes(client_for({1: p1, 2: p2}), _ONE_PIECE)
        assert listing.complete
        assert len(listing.episodes) == 100
        assert {"page": "episodes", "counter": "total", "expected": 101, "actual": 100} in _events(
            diag_events, "shinden_count_mismatch"
        )

    def test_rows_short_of_counter_are_incomplete(self, shinden_html, client_for, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        p2 = _truncated(_paged(_with_links(shinden_html("episodes_titles_p2.html")), total=200, last_page=2), keep=50)
        listing = list_episodes(client_for({1: p1, 2: p2}), _ONE_PIECE)
        assert (listing.complete, listing.incomplete_reason) == (False, "count_mismatch")
        assert len(listing.episodes) == 150
        assert {"page": "episodes", "counter": "total", "expected": 200, "actual": 150} in _events(
            diag_events, "shinden_count_mismatch"
        )
        assert _events(diag_events, "shinden_episodes_incomplete") == [
            {"id": "12", "fetched": 2, "total": 2, "reason": "count_mismatch"}
        ]

    def test_single_page_short_of_counter_is_incomplete(self, shinden_html, client_for):
        html = shinden_html("episodes_frieren.html").replace('ep-list__count">28', 'ep-list__count">40')
        assert list_episodes(client_for({1: html}), _FRIEREN).incomplete_reason == "count_mismatch"

    def test_one_row_short_of_counter_is_tolerated(self, shinden_html, client_for):
        html = shinden_html("episodes_frieren.html").replace('ep-list__count">28', 'ep-list__count">29')
        assert list_episodes(client_for({1: html}), _FRIEREN).complete

    def test_rows_above_counter_are_logged_not_incomplete(self, shinden_html, client_for, diag_events):
        html = shinden_html("episodes_frieren.html").replace('ep-list__count">28', 'ep-list__count">20')
        assert list_episodes(client_for({1: html}), _FRIEREN).complete
        assert _events(diag_events, "shinden_count_mismatch") == [
            {"page": "episodes", "counter": "total", "expected": 20, "actual": 28}
        ]

    def test_unavailable_rows_count_towards_counter(self, shinden_html, client_for):
        p1 = _paged(shinden_html("episodes_titles_p1.html"), total=200, last_page=2)
        p2 = _truncated(_paged(shinden_html("episodes_titles_p2.html"), total=200, last_page=2), keep=50)
        assert list_episodes(client_for({1: p1, 2: p2}), _ONE_PIECE).incomplete_reason == "count_mismatch"

    def test_total_shortfall_tolerance_is_one_row(self):
        assert series.MAX_TOTAL_SHORTFALL == 1

    def test_overlap_tolerance_is_one_row(self):
        assert series.MAX_PAGE_OVERLAP == 1

    def test_page_limit_is_thirty(self):
        assert MAX_EPISODE_PAGES == 30

    def test_page_limit_stops_fetching_and_marks_incomplete(self, shinden_html, client_for, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=4000, last_page=40)
        p2 = _paged(_with_links(shinden_html("episodes_titles_p2.html")), total=4000, last_page=40)
        client = client_for({1: p1} | {n: _shifted(p2, 10_000 * n) for n in range(2, 41)})
        listing = list_episodes(client, _ONE_PIECE)
        assert client.get.call_count == MAX_EPISODE_PAGES
        assert listing.complete is False and len(listing.episodes) == 30 * 100
        assert (listing.complete, listing.incomplete_reason) == (False, "page_limit")
        assert (listing.pages_fetched, listing.pages_total) == (30, 40)
        assert _events(diag_events, "shinden_episodes_incomplete") == [
            {"id": "12", "fetched": 30, "total": 40, "reason": "page_limit"}
        ]

    def test_page_count_from_counter_when_nav_is_short(self, shinden_html, client_for, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=1)
        p2 = _paged(_with_links(shinden_html("episodes_titles_p2.html")), total=200, last_page=1)
        listing = list_episodes(client_for({1: p1, 2: p2}), _ONE_PIECE)
        assert listing.pages_total == 2
        assert {"page": "episodes", "counter": "pages", "expected": 2, "actual": 1} in _events(
            diag_events, "shinden_count_mismatch"
        )

    def test_http_error_after_first_page_is_partial(self, shinden_html, client_for, fake_response, diag_events):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        listing = list_episodes(client_for({1: p1, 2: lambda _url: fake_response("", status=503)}), _ONE_PIECE)
        assert (listing.complete, listing.incomplete_reason) == (False, "http_error")
        assert len(listing.episodes) == 100
        assert _events(diag_events, "shinden_episodes_incomplete")[0]["reason"] == "http_error"

    def test_transport_error_after_first_page_is_partial(self, shinden_html, client_for):
        def _boom(_url):
            raise NetworkError()

        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        listing = list_episodes(client_for({1: p1, 2: _boom}), _ONE_PIECE)
        assert listing.incomplete_reason == "http_error"

    def test_first_page_http_error_propagates(self, client_for, fake_response):
        with pytest.raises(HttpStatusError):
            list_episodes(client_for({1: lambda _url: fake_response("", status=503)}), _FRIEREN)

    def test_drift_on_later_page_raises(self, shinden_html, client_for):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        broken = shinden_html("episodes_titles_p2.html").replace('id="odcinki"', 'id="renamed"')
        with pytest.raises(ParseError):
            list_episodes(client_for({1: p1, 2: broken}), _ONE_PIECE)

    def test_all_rows_invalid_raises(self, shinden_html, client_for):
        html = re.sub(r'ep-row__no">\d+<', 'ep-row__no">?<', shinden_html("episodes_frieren.html"))
        with pytest.raises(ParseError, match="all 28 expected records are invalid"):
            list_episodes(client_for({1: html}), _FRIEREN)

    def test_partial_rows_logged_and_rest_returned(self, shinden_html, client_for, diag_events):
        html = shinden_html("episodes_frieren.html").replace('ep-row__no">28<', 'ep-row__no">?<')
        listing = list_episodes(client_for({1: html}), _FRIEREN)
        assert len(listing.episodes) == 27
        assert _events(diag_events, "shinden_parse_partial") == [
            {"page": "episodes", "found": 28, "valid": 27, "invalid": "bad_number:1", "excluded": None}
        ]

    def test_counter_mismatch_on_complete_listing_is_logged_only(self, shinden_html, client_for, diag_events):
        html = shinden_html("episodes_frieren.html").replace('ep-list__count">28', 'ep-list__count">29')
        listing = list_episodes(client_for({1: html}), _FRIEREN)
        assert listing.complete
        assert {"page": "episodes", "counter": "total", "expected": 29, "actual": 28} in _events(
            diag_events, "shinden_count_mismatch"
        )

    def test_series_404_retries_titles_route(self, shinden_html, fake_response):
        client = MagicMock()
        client.get.side_effect = [fake_response("", status=404), fake_response(shinden_html("episodes_frieren.html"))]
        listing = list_episodes(client, _FRIEREN)
        assert listing.ref.url == f"{SHINDEN_BASE}/titles/61064-sousou-no-frieren"
        assert client.get.call_args_list[1].args[0] == f"{SHINDEN_BASE}/titles/61064-sousou-no-frieren/episodes"

    def test_404_on_both_routes_raises(self, fake_response):
        client = MagicMock()
        client.get.side_effect = [fake_response("", status=404), fake_response("", status=404)]
        with pytest.raises(HttpStatusError):
            list_episodes(client, _FRIEREN)


@pytest.mark.unit
class TestCancellation:
    def _two_pages(self, shinden_html):
        p1 = _paged(_with_links(shinden_html("episodes_titles_p1.html")), total=200, last_page=2)
        return {1: p1, 2: _with_links(shinden_html("episodes_titles_p2.html"))}

    def test_page_delay_goes_through_the_token(self, shinden_html, client_for):
        cancel = RecordingCancel()
        listing = list_episodes(client_for(self._two_pages(shinden_html)), _ONE_PIECE, cancel=cancel)
        assert listing.pages_fetched == 2
        assert cancel.waits == [series._PAGE_DELAY_SEC]

    def test_cancel_during_pagination_stops_before_the_next_page(self, shinden_html, client_for):
        client = client_for(self._two_pages(shinden_html))
        with pytest.raises(OperationCancelled):
            list_episodes(client, _ONE_PIECE, cancel=RecordingCancel(cancel_on_wait=1))
        assert [call.kwargs["params"]["strona"] for call in client.get.call_args_list] == [1]
