"""Tests for shinden/search.py against sanitised Shinden 2.5 search responses."""

from unittest.mock import MagicMock

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import SHINDEN_BASE
from alt_ani_core.errors import OperationCancelled, ParseError
from alt_ani_core.shinden import search
from alt_ani_core.shinden.search import MAX_SEARCH_PAGES, _parse_page, search_series
from alt_ani_core.shinden.utils import ParseTally
from alt_ani_core.testing import RecordingCancel
from alt_ani_core.transport.api import HttpStatusError

_FRIEREN_COUNTER = '<span class="sh-tagi-liczba"><b>4</b> anime</span>'
_EMPTY_MARKER = '<div class="sh-szukaj-brak"><p>Nic nie pasuje.</p></div>'
_PAGE2_URL = f"{SHINDEN_BASE}/series?search=one+piece&page=2"
_MORE = '<div class="sh-tagi-wiecej"><a class="sh-tagi-przycisk" href="/series?search=one+piece&amp;page=3" data-sh-wiecej="">Pokaż więcej</a></div>'
_P1_IDS = ["12", "13184", "59184"]
_P2_IDS = ["34583", "59385", "61035", "71511"]


@pytest.fixture(autouse=True)
def _no_page_delay(monkeypatch):
    monkeypatch.setattr(CancelToken, "wait", lambda _self, _seconds: None)


@pytest.fixture
def client_for(fake_response):
    """Client whose GET answers page 1 by its search params and later pages by their full URL."""

    def _make(page1: str, later: dict | None = None):
        later = later or {}

        def _get(url, params=None):
            body = page1 if params is not None else later[url]
            return body(url) if callable(body) else fake_response(body)

        client = MagicMock()
        client.get.side_effect = _get
        return client

    return _make


def _with_more(html: str) -> str:
    return html.replace("</ol>", "</ol>" + _MORE, 1)


def _without_rows(html: str) -> str:
    return html.replace('class="sh-tagi-wynik"', 'class="sh-renamed-row"')


def _event(events, name):
    return [fields for event, fields in events if event == name]


def _parse_results(html: str):
    tally = ParseTally("search")
    hits = _parse_page(html, tally, set(), first=True).hits
    tally.finish()
    return hits


@pytest.mark.unit
class TestParseResultsFixtures:
    def test_frieren_hits_in_site_order(self, shinden_html):
        hits = _parse_results(shinden_html("search_frieren.html"))
        assert [h.id for h in hits] == ["61064", "68641", "66087", "72659"]

    def test_hit_fields(self, shinden_html):
        hit = _parse_results(shinden_html("search_frieren.html"))[0]
        assert hit.slug == "sousou-no-frieren"
        assert hit.title == "Sousou no Frieren"
        assert hit.series_type == "TV"
        assert hit.url == f"{SHINDEN_BASE}/series/61064-sousou-no-frieren"

    def test_title_comes_from_name_span_not_whole_card(self, shinden_html):
        titles = [h.title for h in _parse_results(shinden_html("search_frieren.html"))]
        assert "Sousou no Frieren: ●● no Mahou" in titles
        assert not any("#" in t or "Ocena" in t for t in titles)

    def test_titles_route_hits_kept_with_their_own_url(self, shinden_html):
        hits = _parse_results(shinden_html("search_mixed.html"))
        one_piece = next(h for h in hits if h.id == "12")
        assert one_piece.url == f"{SHINDEN_BASE}/titles/12-one-piece"
        assert {h.url.split("/")[3] for h in hits} == {"series", "titles"}

    def test_mixed_order_preserved(self, shinden_html):
        hits = _parse_results(shinden_html("search_mixed.html"))
        assert hits[0].id == "12"
        assert len(hits) == 6

    def test_empty_search_returns_empty_list(self, shinden_html, diag_events):
        assert _parse_results(shinden_html("search_empty.html")) == []
        assert diag_events == []

    def test_empty_list_with_zero_counter_returns_empty(self, shinden_html, diag_events):
        html = _without_rows(shinden_html("search_frieren.html")).replace("<b>4</b>", "<b>0</b>")
        assert _parse_results(html) == []
        assert diag_events == []

    def test_empty_list_with_marker_and_no_counter_returns_empty(self, shinden_html):
        html = _without_rows(shinden_html("search_frieren.html")).replace(_FRIEREN_COUNTER, "")
        assert _parse_results(html.replace("</ol>", "</ol>" + _EMPTY_MARKER)) == []

    def test_healthy_page_emits_no_diagnostics(self, shinden_html, diag_events):
        _parse_results(shinden_html("search_mixed.html"))
        assert diag_events == []


@pytest.mark.unit
class TestParseResultsDrift:
    def test_missing_list_without_empty_marker_raises(self, shinden_html):
        html = shinden_html("search_frieren.html").replace("sh-tagi-wyniki", "sh-renamed-list")
        with pytest.raises(ParseError, match="search"):
            _parse_results(html)

    def test_counter_with_no_rows_raises(self, shinden_html):
        html = shinden_html("search_frieren.html").replace('class="sh-tagi-wynik"', 'class="sh-renamed-row"')
        with pytest.raises(ParseError, match="4 results"):
            _parse_results(html)

    @pytest.mark.parametrize(
        "counter",
        [
            pytest.param("", id="missing"),
            pytest.param('<span class="sh-tagi-liczba"><b>?</b> anime</span>', id="unrecognised"),
        ],
    )
    def test_empty_list_without_counter_or_marker_raises(self, shinden_html, counter):
        html = _without_rows(shinden_html("search_frieren.html")).replace(_FRIEREN_COUNTER, counter)
        with pytest.raises(ParseError, match="counter missing or unrecognised"):
            _parse_results(html)

    def test_empty_list_with_marker_but_positive_counter_raises(self, shinden_html):
        html = _without_rows(shinden_html("search_frieren.html")).replace("</ol>", "</ol>" + _EMPTY_MARKER)
        with pytest.raises(ParseError, match="4 results"):
            _parse_results(html)

    def test_unrelated_page_raises_instead_of_empty(self):
        with pytest.raises(ParseError):
            _parse_results("<html><head><title>Shinden</title></head><body><p>maintenance</p></body></html>")

    def test_one_broken_row_is_skipped_and_logged(self, shinden_html, diag_events):
        html = shinden_html("search_frieren.html").replace(
            'href="/series/68641-sousou-no-frieren-2nd-season"', 'href="/somewhere/else"', 1
        )
        hits = _parse_results(html)
        assert [h.id for h in hits] == ["61064", "66087", "72659"]
        (partial,) = _event(diag_events, "shinden_parse_partial")
        assert partial == {"page": "search", "found": 4, "valid": 3, "invalid": "bad_href:1", "excluded": None}

    def test_all_rows_broken_raises(self, shinden_html):
        html = shinden_html("search_frieren.html").replace('<a href="/series/', '<a href="/moved/')
        with pytest.raises(ParseError, match="all 4 expected records are invalid"):
            _parse_results(html)

    def test_duplicates_are_excluded_not_invalid(self, shinden_html, diag_events):
        html = shinden_html("search_frieren.html").replace(
            "/series/68641-sousou-no-frieren-2nd-season", "/series/61064-sousou-no-frieren"
        )
        hits = _parse_results(html)
        assert [h.id for h in hits] == ["61064", "66087", "72659"]
        assert _event(diag_events, "shinden_parse_partial") == []

    def test_more_rows_than_counter_logs_mismatch(self, shinden_html, diag_events):
        html = shinden_html("search_frieren.html").replace("<b>4</b>", "<b>2</b>")
        assert len(_parse_results(html)) == 4
        assert _event(diag_events, "shinden_count_mismatch") == [
            {"page": "search", "counter": "total", "expected": 2, "actual": 4}
        ]

    def test_cloudflare_challenge_raises_distinct_error(self):
        html = "<html><head><title>Just a moment...</title></head><body></body></html>"
        with pytest.raises(ParseError, match="Cloudflare challenge"):
            _parse_results(html)


@pytest.mark.unit
class TestSearchSeries:
    def test_requests_search_param(self, shinden_html, fake_response):
        client = MagicMock()
        client.get.return_value = fake_response(shinden_html("search_frieren.html"))
        results = search_series(client, "frieren")
        client.get.assert_called_once_with(f"{SHINDEN_BASE}/series", params={"search": "frieren"})
        assert len(results.hits) == 4

    def test_single_page_is_complete(self, shinden_html, client_for, diag_events):
        results = search_series(client_for(shinden_html("search_frieren.html")), "frieren")
        assert (results.complete, results.total, results.pages_fetched) == (True, 4, 1)
        assert diag_events == []

    def test_first_page_http_error_propagates(self, client_for, fake_response):
        client = client_for(lambda _url: fake_response("", status=503))
        with pytest.raises(HttpStatusError):
            search_series(client, "frieren")


@pytest.mark.unit
class TestSearchPagination:
    def _two_pages(self, shinden_html):
        return shinden_html("search_mixed.html"), {_PAGE2_URL: shinden_html("search_mixed_p2.html")}

    def test_max_pages_is_two(self):
        assert MAX_SEARCH_PAGES == 2

    def test_follows_show_more_link_and_keeps_server_order(self, shinden_html, client_for):
        page1, later = self._two_pages(shinden_html)
        client = client_for(page1, later)
        results = search_series(client, "one piece")
        ids = [h.id for h in results.hits]
        assert ids[:3] == _P1_IDS
        assert ids[-4:] == _P2_IDS
        assert len(ids) == 10 == len(set(ids))
        assert client.get.call_args_list[1].args == (_PAGE2_URL,)

    def test_missing_show_more_on_last_page_ends_complete(self, shinden_html, client_for, diag_events):
        page1, later = self._two_pages(shinden_html)
        results = search_series(client_for(page1, later), "one piece")
        assert (results.complete, results.incomplete_reason, results.pages_fetched) == (True, None, 2)
        assert _event(diag_events, "shinden_search_incomplete") == []

    def test_counter_matching_hits_emits_no_diagnostics(self, shinden_html, client_for, diag_events):
        page1, later = self._two_pages(shinden_html)
        later = {url: html.replace("<b>85</b>", "<b>10</b>") for url, html in later.items()}
        search_series(client_for(page1.replace("<b>85</b>", "<b>10</b>"), later), "one piece")
        assert diag_events == []

    def test_counter_short_of_hits_is_logged_but_complete(self, shinden_html, client_for, diag_events):
        page1, later = self._two_pages(shinden_html)
        results = search_series(client_for(page1, later), "one piece")
        assert results.complete
        assert _event(diag_events, "shinden_count_mismatch") == [
            {"page": "search", "counter": "total", "expected": 85, "actual": 10}
        ]

    def test_positive_counter_without_show_more_does_not_fetch_more(self, shinden_html, client_for):
        page1 = shinden_html("search_mixed.html").replace('data-sh-wiecej=""', "")
        client = client_for(page1)
        results = search_series(client, "one piece")
        assert (results.complete, results.total, client.get.call_count) == (True, 85, 1)

    def test_duplicates_across_pages_are_dropped(self, shinden_html, client_for):
        page1, later = self._two_pages(shinden_html)
        later = {_PAGE2_URL: later[_PAGE2_URL].replace("/titles/34583-one-piece-cry-heart", "/titles/12-one-piece")}
        results = search_series(client_for(page1, later), "one piece")
        ids = [h.id for h in results.hits]
        assert ids.count("12") == 1
        assert "34583" not in ids
        assert results.complete

    def test_page_of_only_repeats_is_incomplete(self, shinden_html, client_for, diag_events):
        page1 = shinden_html("search_mixed.html")
        results = search_series(client_for(page1, {_PAGE2_URL: page1}), "one piece")
        assert (results.complete, results.incomplete_reason) == (False, "repeated_page")
        assert len(results.hits) == 6

    def test_empty_later_page_is_empty_page_not_drift(self, shinden_html, client_for, diag_events):
        empty = shinden_html("search_empty.html").replace("<b>0</b>", "<b>85</b>")
        results = search_series(client_for(shinden_html("search_mixed.html"), {_PAGE2_URL: empty}), "one piece")
        assert (results.complete, results.incomplete_reason, results.pages_fetched) == (False, "empty_page", 2)
        assert len(results.hits) == 6
        assert _event(diag_events, "shinden_search_incomplete") == [
            {"fetched": 2, "hits": 6, "total": 85, "reason": "empty_page"}
        ]

    def test_empty_first_page_with_positive_counter_still_raises(self, shinden_html, client_for):
        empty = shinden_html("search_empty.html").replace("<b>0</b>", "<b>85</b>")
        with pytest.raises(ParseError, match="search"):
            search_series(client_for(empty), "one piece")

    def test_later_page_without_list_or_marker_raises(self, shinden_html, client_for):
        broken = shinden_html("search_mixed_p2.html").replace("sh-tagi-wyniki", "sh-renamed-list")
        with pytest.raises(ParseError, match="search"):
            search_series(client_for(shinden_html("search_mixed.html"), {_PAGE2_URL: broken}), "one piece")

    def test_show_more_on_last_allowed_page_is_page_limit(self, shinden_html, client_for, diag_events):
        page1, later = self._two_pages(shinden_html)
        later = {_PAGE2_URL: _with_more(later[_PAGE2_URL])}
        client = client_for(page1, later)
        results = search_series(client, "one piece")
        assert (results.complete, results.incomplete_reason, results.pages_fetched) == (False, "page_limit", 2)
        assert len(results.hits) == 10
        assert client.get.call_count == MAX_SEARCH_PAGES
        assert _event(diag_events, "shinden_search_incomplete") == [
            {"fetched": 2, "hits": 10, "total": 85, "reason": "page_limit"}
        ]

    def test_http_error_on_later_page_keeps_earlier_hits(self, shinden_html, client_for, fake_response):
        later = {_PAGE2_URL: lambda _url: fake_response("", status=502)}
        results = search_series(client_for(shinden_html("search_mixed.html"), later), "one piece")
        assert (results.complete, results.incomplete_reason, results.pages_fetched) == (False, "http_error", 1)
        assert [h.id for h in results.hits][:3] == _P1_IDS

    def test_show_more_link_off_site_raises(self, shinden_html, client_for):
        page1 = shinden_html("search_mixed.html").replace(
            'href="/series?search=one+piece&amp;page=2" data-sh-wiecej', 'href="https://evil.example/series" data-sh-wiecej'
        )
        with pytest.raises(ParseError, match="next-page link"):
            search_series(client_for(page1), "one piece")

    def test_page_delay_goes_through_the_token(self, shinden_html, client_for):
        cancel = RecordingCancel()
        page1, later = self._two_pages(shinden_html)
        search_series(client_for(page1, later), "one piece", cancel=cancel)
        assert cancel.waits == [search._PAGE_DELAY_SEC]

    def test_cancel_stops_before_the_next_page(self, shinden_html, client_for):
        page1, later = self._two_pages(shinden_html)
        client = client_for(page1, later)
        with pytest.raises(OperationCancelled):
            search_series(client, "one piece", cancel=RecordingCancel(cancel_on_wait=1))
        assert client.get.call_count == 1
