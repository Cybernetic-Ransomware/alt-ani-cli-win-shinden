"""Tests for shinden/metadata.py — parser functions use inline HTML fixtures."""

import httpx
import pytest
from selectolax.parser import HTMLParser

from alt_ani_cli.errors import ShindenError
from alt_ani_cli.shinden.metadata import (
    _extract_relation,
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
        # No dt/dd, but date is near the label in plain text.
        tree = _tree("<div>Data emisji: 15.03.2005</div>")
        date, sort = _parse_air_date(tree)
        assert date == "15.03.2005"
        assert sort == (2005, 3, 15)


@pytest.mark.unit
class TestParseDescription:
    def test_joins_paragraphs(self):
        tree = _tree(
            "<section itemprop='description'>"
            "<p>Pierwsza część.</p>"
            "<p>Druga część.</p>"
            "</section>"
        )
        assert _parse_description(tree) == "Pierwsza część.\n\nDruga część."

    def test_single_paragraph(self):
        tree = _tree(
            "<section itemprop='description'><p>Jednoakapitowy opis.</p></section>"
        )
        assert _parse_description(tree) == "Jednoakapitowy opis."

    def test_no_p_uses_text(self):
        tree = _tree("<div itemprop='description'>Opis bez paragrafów.</div>")
        assert _parse_description(tree) == "Opis bez paragrafów."

    def test_missing_returns_empty(self):
        tree = _tree("<div class='nothing'></div>")
        assert _parse_description(tree) == ""

    def test_class_description_fallback(self):
        tree = _tree("<div class='description'><p>Opis przez klasę.</p></div>")
        assert _parse_description(tree) == "Opis przez klasę."


@pytest.mark.unit
class TestParseTags:
    _FULL_HTML = (
        "<dl>"
        "<dt>Miejsce i czas:</dt><dd><a>Japonia</a><a>Współczesność</a></dd>"
        "<dt>Gatunki:</dt><dd><a>Akcja</a><a>Komedia</a></dd>"
        "<dt>Grupy docelowe:</dt><dd><a>Seinen</a></dd>"
        "<dt>Pozostałe tagi:</dt><dd><a>Supermoce</a></dd>"
        "<dt>Rodzaje postaci:</dt><dd><a>Bishoujo</a></dd>"
        "<dt>Pierwowzór:</dt><dd><a>Manga</a></dd>"
        "</dl>"
    )

    def test_concatenated_in_yaml_group_order(self):
        # HTML intentionally has "Miejsce i czas" before "Gatunki" —
        # result must follow _TAG_GROUP_ORDER regardless of HTML order.
        tree = _tree(self._FULL_HTML)
        tags = _parse_tags(tree)
        assert list(tags) == [
            "Akcja", "Komedia",
            "Seinen",
            "Supermoce",
            "Bishoujo",
            "Japonia", "Współczesność",
            "Manga",
        ]

    def test_missing_group_skipped(self):
        tree = _tree(
            "<dl>"
            "<dt>Gatunki:</dt><dd><a>Akcja</a></dd>"
            "</dl>"
        )
        assert list(_parse_tags(tree)) == ["Akcja"]

    def test_empty_page_returns_empty_tuple(self):
        assert _parse_tags(_tree("<div></div>")) == ()


@pytest.mark.unit
class TestParseRelated:
    _MIXED_HTML = (
        "<dt>Powiązane serie:</dt>"
        "<dd>"
        "<ul>"
        "<li><a href='/series/456-dragon-destiny'>Ikkitousen: Dragon Destiny</a> Anime Sequel</li>"
        "<li><a href='/series/789-dragon-destiny-manga'>Ikkitousen Manga</a> Manga Adaptacja</li>"
        "</ul>"
        "</dd>"
    )

    def test_filters_out_manga(self):
        tree = _tree(self._MIXED_HTML)
        related = _parse_related(tree)
        ids = [r.id for r in related]
        assert "456" in ids
        assert "789" not in ids

    def test_extracts_relation_label(self):
        tree = _tree(self._MIXED_HTML)
        related = _parse_related(tree)
        assert len(related) >= 1
        assert related[0].relation == "Sequel"

    def test_extracts_title_and_url(self):
        tree = _tree(self._MIXED_HTML)
        r = _parse_related(tree)[0]
        assert r.title == "Ikkitousen: Dragon Destiny"
        assert "/series/456-dragon-destiny" in r.url

    def test_empty_section_returns_empty_tuple(self):
        assert _parse_related(_tree("<div></div>")) == ()

    def test_no_related_dt_returns_empty_tuple(self):
        assert _parse_related(_tree("<dl><dt>Gatunki:</dt><dd><a>Akcja</a></dd></dl>")) == ()


@pytest.mark.unit
class TestFetchSeriesMetadata:
    def test_age_gate_raises(self, monkeypatch):
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

    def test_returns_metadata_on_valid_page(self, monkeypatch):
        html = (
            "<html><body>"
            "<dl>"
            "<dt>Data emisji:</dt><dd>30.07.2003</dd>"
            "<dt>Gatunki:</dt><dd><a>Akcja</a></dd>"
            "</dl>"
            "<section itemprop='description'><p>Opis serii.</p></section>"
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


@pytest.mark.unit
class TestExtractRelation:
    @pytest.mark.parametrize(
        "label",
        ["Sequel", "Prequel", "Spin-off", "Adaptacja", "Inna wersja", "Alternatywna wersja", "Inne"],
    )
    def test_finds_each_known_label(self, label):
        assert _extract_relation(f"Ikkitousen Anime {label}", "Ikkitousen") == label

    def test_fallback_strips_title_and_anime(self):
        result = _extract_relation("Ikkitousen Anime Specjalna wersja", "Ikkitousen")
        assert "Specjalna wersja" in result

    def test_fallback_returns_dash_when_empty_remainder(self):
        assert _extract_relation("Ikkitousen Anime Manga", "Ikkitousen") == "—"


@pytest.mark.unit
class TestParseRelatedExtended:
    def test_entry_without_anime_label_is_skipped(self):
        html = (
            "<dt>Powiązane serie:</dt>"
            "<dd><ul>"
            "<li><a href='/series/10-only-manga'>Only Manga</a> Manga Adaptacja</li>"
            "</ul></dd>"
        )
        assert _parse_related(_tree(html)) == ()

    def test_link_without_series_pattern_is_skipped(self):
        html = (
            "<dt>Powiązane serie:</dt>"
            "<dd><ul>"
            "<li><a href='/episode/999-bad-link'>Bad Link</a> Anime Sequel</li>"
            "</ul></dd>"
        )
        assert _parse_related(_tree(html)) == ()

    def test_section_related_fallback_container(self):
        html = (
            "<section class='related'>"
            "<a href='/series/20-test-anime'>Test Anime</a> Anime Prequel"
            "</section>"
        )
        ids = [r.id for r in _parse_related(_tree(html))]
        assert "20" in ids
