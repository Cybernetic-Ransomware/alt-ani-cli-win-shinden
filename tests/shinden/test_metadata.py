"""Tests for shinden/metadata.py — parser functions use inline HTML fixtures."""

import httpx
import pytest
from selectolax.parser import HTMLParser

from alt_ani_cli.errors import ShindenError
from alt_ani_cli.shinden.metadata import (
    _parse_air_date,
    _parse_description,
    _parse_related,
    _parse_tags,
    fetch_series_metadata,
)
from alt_ani_cli.shinden.models import SeriesRef

_BASE = "https://shinden.pl"


def _ref(series_id: str = "234", slug: str = "ikkitousen") -> SeriesRef:
    return SeriesRef(id=series_id, slug=slug, title="Ikkitousen", url=f"{_BASE}/series/{series_id}-{slug}")


def _tree(html: str) -> HTMLParser:
    return HTMLParser(html)


@pytest.mark.unit
class TestParseAirDate:
    def test_extracts_dd_mm_yyyy(self):
        tree = _tree(
            "<dl>"
            "<dt>Data emisji:</dt><dd>30.07.2003</dd>"
            "</dl>"
        )
        date, sort = _parse_air_date(tree)
        assert date == "30.07.2003"
        assert sort == (2003, 7, 30)

    def test_missing_returns_none(self):
        tree = _tree("<dl><dt>Typ:</dt><dd>Anime</dd></dl>")
        date, sort = _parse_air_date(tree)
        assert date is None
        assert sort is None

    def test_label_without_trailing_colon(self):
        tree = _tree(
            "<dl>"
            "<dt>Data emisji</dt><dd>01.01.2020</dd>"
            "</dl>"
        )
        date, sort = _parse_air_date(tree)
        assert date == "01.01.2020"
        assert sort == (2020, 1, 1)

    def test_fallback_text_scan(self):
        tree = _tree("<div>Data emisji: 15.03.2005</div>")
        date, sort = _parse_air_date(tree)
        assert date == "15.03.2005"
        assert sort == (2005, 3, 15)


@pytest.mark.unit
class TestParseDescription:
    def test_joins_paragraphs(self):
        tree = _tree(
            "<div id='description'>"
            "<p>Pierwsza część.</p>"
            "<p>Druga część.</p>"
            "</div>"
        )
        assert _parse_description(tree) == "Pierwsza część.\n\nDruga część."

    def test_single_paragraph(self):
        tree = _tree("<div id='description'><p>Jednoakapitowy opis.</p></div>")
        assert _parse_description(tree) == "Jednoakapitowy opis."

    def test_no_p_uses_text(self):
        tree = _tree("<div id='description'>Opis bez paragrafów.</div>")
        assert _parse_description(tree) == "Opis bez paragrafów."

    def test_missing_returns_empty(self):
        tree = _tree("<div class='nothing'></div>")
        assert _parse_description(tree) == ""

    def test_class_fallback(self):
        tree = _tree("<div class='title-full-description'><p>Opis przez klasę.</p></div>")
        assert _parse_description(tree) == "Opis przez klasę."

    def test_empty_div_returns_empty(self):
        tree = _tree("<div id='description'></div>")
        assert _parse_description(tree) == ""


def _tags_table(*rows: tuple[str, list[str]]) -> str:
    """Build a minimal data-view-table with given (label, [tag, ...]) rows."""
    trs = ""
    for label, tags in rows:
        lis = "".join(f"<li><a>{t}</a></li>" for t in tags)
        trs += f"<tr><td>{label}:</td><td><ul class='tags'>{lis}</ul></td></tr>"
    return f"<table class='data-view-table'><tbody>{trs}</tbody></table>"


@pytest.mark.unit
class TestParseTags:
    def test_concatenated_in_yaml_group_order(self):
        html = _tags_table(
            ("Miejsce i czas", ["Japonia", "Współczesność"]),
            ("Gatunki", ["Akcja", "Komedia"]),
            ("Grupy docelowe", ["Seinen"]),
            ("Pozostałe tagi", ["Supermoce"]),
            ("Rodzaje postaci", ["Bishoujo"]),
            ("Pierwowzór", ["Manga"]),
        )
        tags = _parse_tags(_tree(html))
        assert list(tags) == [
            "Akcja", "Komedia",
            "Seinen",
            "Supermoce",
            "Bishoujo",
            "Japonia", "Współczesność",
            "Manga",
        ]

    def test_missing_group_skipped(self):
        html = _tags_table(("Gatunki", ["Akcja"]))
        assert list(_parse_tags(_tree(html))) == ["Akcja"]

    def test_no_table_returns_empty_tuple(self):
        assert _parse_tags(_tree("<div></div>")) == ()

    def test_unknown_row_label_ignored(self):
        html = _tags_table(("Gatunki", ["Akcja"]), ("Nieznane", ["X"]))
        assert list(_parse_tags(_tree(html))) == ["Akcja"]


def _related_section(*entries: tuple[str, str, str, str]) -> str:
    """Build a minimal related-series section.

    entries: (href, title, kind, relation)
    """
    lis = ""
    for href, title, kind, relation in entries:
        lis += (
            f"<li class='relation_t2t'><figure>"
            f"<figcaption><a href='{href}'>{title}</a></figcaption>"
            f"<figcaption class='figure-type'>{kind}</figcaption>"
            f"<figcaption class='figure-type'>{relation}</figcaption>"
            f"</figure></li>"
        )
    return (
        "<section class='box'>"
        "<h2 class='box-title h4'>Powiązane Serie</h2>"
        f"<ul class='figure-list'>{lis}</ul>"
        "</section>"
    )


@pytest.mark.unit
class TestParseRelated:
    def test_filters_out_manga(self):
        html = _related_section(
            ("/series/456-dragon-destiny", "Ikkitousen: Dragon Destiny", "Anime", "Sequel"),
            ("/manga/789-dragon-destiny-manga", "Ikkitousen Manga", "Manga", "Adaptacja"),
        )
        related = _parse_related(_tree(html))
        ids = [r.id for r in related]
        assert "456" in ids
        assert "789" not in ids

    def test_extracts_relation_label(self):
        html = _related_section(
            ("/series/456-dragon-destiny", "Ikkitousen: Dragon Destiny", "Anime", "Sequel"),
        )
        related = _parse_related(_tree(html))
        assert len(related) == 1
        assert related[0].relation == "Sequel"

    def test_extracts_title_and_url(self):
        html = _related_section(
            ("/series/456-dragon-destiny", "Ikkitousen: Dragon Destiny", "Anime", "Sequel"),
        )
        r = _parse_related(_tree(html))[0]
        assert r.title == "Ikkitousen: Dragon Destiny"
        assert "/series/456-dragon-destiny" in r.url

    def test_no_related_section_returns_empty_tuple(self):
        assert _parse_related(_tree("<div></div>")) == ()

    def test_link_without_series_pattern_is_skipped(self):
        html = _related_section(
            ("/episode/999-bad-link", "Bad Link", "Anime", "Sequel"),
        )
        assert _parse_related(_tree(html)) == ()

    def test_entry_without_anime_kind_is_skipped(self):
        html = _related_section(
            ("/series/10-only-manga", "Only Manga", "Manga", "Adaptacja"),
        )
        assert _parse_related(_tree(html)) == ()

    def test_relation_label_preserved_as_is(self):
        html = _related_section(
            ("/series/20-test", "Test", "Anime", "Alternatywna Wersja"),
        )
        r = _parse_related(_tree(html))[0]
        assert r.relation == "Alternatywna Wersja"


@pytest.mark.unit
class TestFetchSeriesMetadata:
    def test_age_gate_raises(self):
        class _FakeResp:
            text = "musisz mieć ukończone 18 lat aby obejrzeć tę serię"
            status_code = 200

            def raise_for_status(self):
                pass

        class _FakeClient:
            def get(self, url):
                return _FakeResp()

        with pytest.raises(ShindenError):
            fetch_series_metadata(_FakeClient(), _ref())

    def test_returns_metadata_on_valid_page(self):
        html = (
            "<html><body>"
            "<dl class='info-aside-list'>"
            "<dt>Data emisji:</dt><dd>30.07.2003</dd>"
            "</dl>"
            "<table class='data-view-table'><tbody>"
            "<tr><td>Gatunki:</td><td><ul class='tags'><li><a>Akcja</a></li></ul></td></tr>"
            "</tbody></table>"
            "<div id='description'><p>Opis serii.</p></div>"
            "</body></html>"
        )

        class _FakeResp:
            text = html
            status_code = 200

            def raise_for_status(self):
                pass

        class _FakeClient:
            def get(self, url):
                return _FakeResp()

        meta = fetch_series_metadata(_FakeClient(), _ref())
        assert meta.air_date == "30.07.2003"
        assert meta.air_date_sort == (2003, 7, 30)
        assert meta.description == "Opis serii."
        assert "Akcja" in meta.tags

    def test_http_error_propagates(self):
        class _FakeResp:
            status_code = 404
            text = ""

            def raise_for_status(self):
                request = httpx.Request("GET", "https://shinden.pl/series/1-test")
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("404", request=request, response=response)

        class _FakeClient:
            def get(self, url):
                return _FakeResp()

        with pytest.raises(httpx.HTTPStatusError):
            fetch_series_metadata(_FakeClient(), _ref())
