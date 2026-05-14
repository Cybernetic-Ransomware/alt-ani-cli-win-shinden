"""Tests for shinden/search.py — HTML result parser."""

import pytest

from alt_ani_cli.shinden.search import _parse_results


def _row(series_id, slug, title, series_type="TV", genres=("Akcja",)):
    genre_links = "".join(
        f'<li class="genre-col"><a href="/series?genres[]={g}">{g}</a></li>'
        for g in genres
    )
    return (
        f'<ul class="div-row">'
        f'<li class="title-kind-col">'
        f'<a href="/series/{series_id}-{slug}">{title}</a>'
        f'</li>'
        f'<li class="type-col">{series_type}</li>'
        f'<li class="episodes-col">12</li>'
        f'{genre_links}'
        f'</ul>'
    )


_HEADER_ROW = (
    '<ul class="div-row">'
    '<li class="title-kind-col"><a href="/series">Nazwa</a></li>'
    '<li class="type-col"><a href="/series?type=TV">Typ</a></li>'
    '</ul>'
)

_SINGLE_HTML = f"<html><body>{_row('65137','fate-strange-fake','Fate/strange Fake')}</body></html>"
_MULTI_HTML = (
    "<html><body>"
    + _HEADER_ROW
    + _row("65137", "fate-strange-fake", "Fate/strange Fake", "TV")
    + _row("12345", "demon-slayer", "Demon Slayer", "TV")
    + "</body></html>"
)
_GENRES_HTML = (
    "<html><body>"
    + _row("65137", "fate-strange-fake", "Fate/strange Fake", "TV", ("Akcja", "Magia"))
    + "</body></html>"
)
_EMPTY_HTML = "<html><body><p>Brak wynikow.</p></body></html>"
_DUPE_HTML = (
    "<html><body>"
    + _row("65137", "fate-strange-fake", "Fate/strange Fake")
    + _row("65137", "fate-strange-fake", "Fate/strange Fake (kopia)")
    + "</body></html>"
)


@pytest.mark.unit
class TestParseResults:
    def test_single_result(self):
        hits = _parse_results(_SINGLE_HTML)
        assert len(hits) == 1
        assert hits[0].id == "65137"
        assert hits[0].title == "Fate/strange Fake"

    def test_title_extracted_correctly(self):
        assert _parse_results(_SINGLE_HTML)[0].title == "Fate/strange Fake"

    def test_url_is_absolute(self):
        assert _parse_results(_SINGLE_HTML)[0].url == "https://shinden.pl/series/65137-fate-strange-fake"

    def test_slug_extracted(self):
        assert _parse_results(_SINGLE_HTML)[0].slug == "fate-strange-fake"

    def test_series_type_extracted(self):
        fate = next(h for h in _parse_results(_MULTI_HTML) if h.id == "65137")
        assert fate.series_type == "TV"

    def test_header_row_skipped(self):
        ids = {h.id for h in _parse_results(_MULTI_HTML)}
        assert ids == {"65137", "12345"}

    def test_genre_links_not_treated_as_series(self):
        hits = _parse_results(_GENRES_HTML)
        assert len(hits) == 1
        assert hits[0].id == "65137"

    def test_deduplication(self):
        assert len(_parse_results(_DUPE_HTML)) == 1

    def test_empty_page(self):
        assert _parse_results(_EMPTY_HTML) == []

    def test_multiple_results_order(self):
        ids = [h.id for h in _parse_results(_MULTI_HTML)]
        assert "65137" in ids and "12345" in ids
