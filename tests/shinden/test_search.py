"""Tests for shinden/search.py against sanitised Shinden 2.5 search responses."""

from unittest.mock import MagicMock

import pytest

from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.errors import ParseError
from alt_ani_cli.shinden.search import _parse_results, search_series

_FRIEREN_COUNTER = '<span class="sh-tagi-liczba"><b>4</b> anime</span>'
_EMPTY_MARKER = '<div class="sh-szukaj-brak"><p>Nic nie pasuje.</p></div>'


def _without_rows(html: str) -> str:
    return html.replace('class="sh-tagi-wynik"', 'class="sh-renamed-row"')


def _event(events, name):
    return [fields for event, fields in events if event == name]


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
        hits = search_series(client, "frieren")
        client.get.assert_called_once_with(f"{SHINDEN_BASE}/series", params={"search": "frieren"})
        assert len(hits) == 4
