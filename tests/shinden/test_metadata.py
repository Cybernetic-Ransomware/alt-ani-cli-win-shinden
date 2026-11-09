"""Tests for shinden/metadata.py against sanitised Shinden 2.5 series-page responses."""

import re
from unittest.mock import MagicMock

import pytest
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError
from selectolax.parser import HTMLParser

from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.errors import ParseError, ShindenError
from alt_ani_cli.models import SeriesRef
from alt_ani_cli.shinden.metadata import (
    _TAG_GROUP_ORDER,
    _parse_air_date,
    _parse_description,
    fetch_series_metadata,
)

_REF = SeriesRef(
    id="61064", slug="sousou-no-frieren", title="Sousou No Frieren", url=f"{SHINDEN_BASE}/series/61064-sousou-no-frieren"
)


def _events(events, name):
    return [fields for event, fields in events if event == name]


def _info(dd: str) -> HTMLParser:
    return HTMLParser(f'<dl class="sh-tp-info"><dt>Typ</dt><dd>TV</dd><dt>Emisja</dt><dd>{dd}</dd></dl>')


def _description(inner: str) -> str:
    return _parse_description(HTMLParser(f'<div id="description" class="sh-tp-desc sh-zwin">{inner}</div>'))


def _emisja(html: str, value: str) -> str:
    return re.sub(r"<dt>Emisja</dt><dd>[^<]*</dd>", f"<dt>Emisja</dt><dd>{value}</dd>", html)


def _rels_section(html: str) -> str:
    return re.search(r'<section class="sh-tp-section" aria-labelledby="sh-tp-h-rels">.*?</section>', html, re.S).group(0)


def _without_rels(html: str) -> str:
    return html.replace(_rels_section(html), "")


@pytest.fixture
def fetch(fake_response):
    def _fetch(html: str):
        client = MagicMock()
        client.get.return_value = fake_response(html)
        return fetch_series_metadata(client, _REF)

    return _fetch


@pytest.mark.unit
class TestParseAirDate:
    def _parse(self, dd: str):
        return _parse_air_date(_info(dd).css_first("dl.sh-tp-info"))

    def test_range_keeps_start_date(self):
        assert self._parse("29.09.2023 – 22.03.2024") == ("29.09.2023", (2023, 9, 29))

    def test_single_full_date(self):
        assert self._parse("20.10.1999") == ("20.10.1999", (1999, 10, 20))

    def test_leap_day_is_valid(self):
        assert self._parse("29.02.2024") == ("29.02.2024", (2024, 2, 29))

    def test_month_and_year_keeps_unknown_day_as_zero(self):
        assert self._parse("10.2027") == ("10.2027", (2027, 10, 0))

    def test_year_only_keeps_unknown_month_and_day_as_zero(self):
        assert self._parse("2027") == ("2027", (2027, 0, 0))

    @pytest.mark.parametrize(
        ("dd", "reason"),
        [
            ("31.13.2023", "bad_date:1"),
            ("31.02.2023", "bad_date:1"),
            ("00.2027", "bad_date:1"),
            ("13.2027", "bad_date:1"),
            ("2023.09.29", "bad_format:1"),
            ("29.09.23", "bad_format:1"),
            ("29.09.2023r.", "bad_format:1"),
        ],
    )
    def test_invalid_date_is_dropped_and_reported(self, diag_events, dd, reason):
        """No year is salvaged from a malformed token — the whole token must match."""
        assert self._parse(dd) == (None, None)
        [event] = _events(diag_events, "shinden_parse_partial")
        assert event["page"] == "metadata:air_date"
        assert event["invalid"] == reason

    @pytest.mark.parametrize("dd", ["?", "", "brak danych"])
    def test_placeholder_without_digits_is_absence(self, diag_events, dd):
        assert self._parse(dd) == (None, None)
        assert diag_events == []

    def test_missing_label_is_absence(self, diag_events):
        info = HTMLParser('<dl class="sh-tp-info"><dt>Typ</dt><dd>TV</dd></dl>').css_first("dl")
        assert _parse_air_date(info) == (None, None)
        assert diag_events == []

    def test_whitespace_between_dt_and_dd(self):
        info = HTMLParser('<dl class="sh-tp-info"><dt>Emisja</dt>\n  <dd>20.10.1999</dd></dl>').css_first("dl")
        assert _parse_air_date(info) == ("20.10.1999", (1999, 10, 20))

    def test_legacy_label_is_not_matched(self):
        info = HTMLParser('<dl class="sh-tp-info"><dt>Data emisji:</dt><dd>30.07.2003</dd></dl>').css_first("dl")
        assert _parse_air_date(info) == (None, None)


@pytest.mark.unit
class TestParseDescription:
    def test_br_splits_paragraphs(self):
        assert _description("Pierwszy.<br />\nDrugi.") == "Pierwszy.\n\nDrugi."

    def test_inline_element_keeps_surrounding_spaces(self):
        assert _description("do piosenki <em>Yuusha</em> zespołu") == "do piosenki Yuusha zespołu"

    def test_inline_element_without_spaces_is_not_padded(self):
        assert _description("pół<em>słowa</em>dalej") == "półsłowadalej"

    def test_nested_inline_elements(self):
        assert _description("a <b>b <i>c</i></b> d") == "a b c d"

    def test_consecutive_separators_give_one_break(self):
        assert _description("Jeden.<br><br />\n<br/>Dwa.") == "Jeden.\n\nDwa."

    def test_leading_trailing_and_whitespace_only_paragraphs_are_dropped(self):
        assert _description("<br>  <br>Treść.<br> \n <br>") == "Treść."

    def test_html_entities_are_decoded(self):
        assert _description("Tom&#039;s &amp; Jerry&quot;s") == "Tom's & Jerry\"s"

    def test_whitespace_runs_are_collapsed(self):
        assert _description("  a \n\t b  ") == "a b"

    def test_tree_is_not_mutated(self):
        tree = HTMLParser('<div id="description">a<br>b</div>')
        before = tree.html
        _parse_description(tree)
        assert tree.html == before

    def test_missing_node_returns_empty(self):
        assert _parse_description(HTMLParser("<div class='nothing'></div>")) == ""

    def test_empty_node_returns_empty(self):
        assert _description("") == ""


@pytest.mark.unit
class TestFrierenFixture:
    def test_air_date_range(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_frieren.html"))
        assert meta.air_date == "29.09.2023"
        assert meta.air_date_sort == (2023, 9, 29)

    def test_description_paragraphs(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_frieren.html"))
        assert meta.description == (
            "Król demonów został pokonany, a zwycięska drużyna bohaterów wraca do domu, zanim się rozpadnie."
            "\n\nPrzed śmiercią Heiter narzuca Frieren młodą uczennicę o imieniu Fern."
        )

    def test_tags_follow_group_order_without_highlight_duplicates(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_frieren.html"))
        assert meta.tags[:5] == ("Przygodowe", "Dramat", "Magia", "Fantasy", "Shounen")
        assert meta.tags[-3:] == ("Podróż", "Jak średniowiecze", "Manga")
        assert len(meta.tags) == 15

    def test_related_keeps_anime_and_drops_manga(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_frieren.html"))
        assert [r.id for r in meta.related] == ["68641", "66087", "66033", "67494"]
        first = meta.related[0]
        assert first.title == "Sousou no Frieren 2nd Season"
        assert first.relation == "Sequel"
        assert first.slug == "sousou-no-frieren-2nd-season"
        assert first.url == f"{SHINDEN_BASE}/series/68641-sousou-no-frieren-2nd-season"

    def test_clean_page_emits_no_diagnostics(self, fetch, shinden_html, diag_events):
        fetch(shinden_html("series_frieren.html"))
        assert diag_events == []


@pytest.mark.unit
class TestTitlesFixture:
    def test_single_air_date(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_titles_motywy.html"))
        assert meta.air_date_sort == (1999, 10, 20)

    def test_motywy_sit_between_target_groups_and_characters(self, fetch, shinden_html):
        tags = fetch(shinden_html("series_titles_motywy.html")).tags
        assert tags.index("Shounen") < tags.index("Supermoce") < tags.index("Broń biała") < tags.index("Ninja")
        assert len(tags) == 42

    def test_related_titles_and_series_routes_are_kept(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_titles_motywy.html"))
        assert [r.url.removeprefix(SHINDEN_BASE) for r in meta.related] == [
            "/titles/438-one-piece-taose-kaizoku-ganzack",
            "/titles/431-one-piece-movie-01",
            "/series/73458-one-piece-uchuu-tte-omoshiree-hoshizora-jima-hen",
            "/series/73685-one-piece-the-planetarium",
        ]

    def test_books_and_manga_are_excluded_silently(self, fetch, shinden_html, diag_events):
        meta = fetch(shinden_html("series_titles_motywy.html"))
        assert not any(r.title in {"One Piece", "One Piece Strong World 0", "Shokugeki no Sanji"} for r in meta.related)
        assert diag_events == []

    def test_tiles_behind_show_all_are_included(self, fetch, shinden_html):
        html = shinden_html("series_titles_motywy.html")
        assert '<details class="sh-tp-more">' in html
        assert "73685" in [r.id for r in fetch(html).related]

    def test_duplicate_tile_is_excluded(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_titles_motywy.html").replace("/series/73685-one-piece-the-planetarium", "/titles/431-x")
        meta = fetch(html)
        assert [r.id for r in meta.related].count("431") == 1
        assert _events(diag_events, "shinden_parse_partial") == []


@pytest.mark.unit
class TestAnnouncementFixture:
    def test_month_year_air_date(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_announcement.html"))
        assert meta.air_date == "10.2027"
        assert meta.air_date_sort == (2027, 10, 0)

    def test_few_tag_groups(self, fetch, shinden_html):
        assert fetch(shinden_html("series_announcement.html")).tags == ("Shounen", "Manga")


@pytest.mark.unit
class TestMusicVideoFixture:
    def test_em_does_not_glue_words(self, fetch, shinden_html):
        meta = fetch(shinden_html("series_music_video.html"))
        assert "do piosenki Yuusha zespołu YOASOBI." in meta.description

    def test_same_day_range(self, fetch, shinden_html):
        assert fetch(shinden_html("series_music_video.html")).air_date_sort == (2023, 9, 29)


@pytest.mark.unit
class TestPageStructure:
    def test_missing_info_list_is_drift(self, fetch, shinden_html):
        html = shinden_html("series_frieren.html").replace('<dl class="sh-tp-info">', '<dl class="sh-tp-dane">')
        with pytest.raises(ParseError, match="dl.sh-tp-info"):
            fetch(html)

    def test_empty_info_list_is_drift(self, fetch, shinden_html):
        html = re.sub(r'(<dl class="sh-tp-info">).*?(</dl>)', r"\1\2", shinden_html("series_frieren.html"), flags=re.S)
        with pytest.raises(ParseError, match="no entries"):
            fetch(html)

    def test_cloudflare_interstitial_is_parse_error(self, fetch):
        with pytest.raises(ParseError):
            fetch("<html><head><title>Just a moment...</title></head><body></body></html>")

    def test_age_gate_raises(self, fetch):
        with pytest.raises(ShindenError):
            fetch("musisz mieć ukończone 18 lat aby obejrzeć tę serię")

    def test_http_error_propagates(self, fake_response):
        client = MagicMock()
        client.get.return_value = fake_response(status=404)
        with pytest.raises(CurlHTTPError):
            fetch_series_metadata(client, _REF)

    def test_requests_ref_url(self, fake_response, shinden_html):
        client = MagicMock()
        client.get.return_value = fake_response(shinden_html("series_frieren.html"))
        fetch_series_metadata(client, _REF)
        client.get.assert_called_once_with(_REF.url)


@pytest.mark.unit
class TestOptionalAbsence:
    """Fields really missing from a recognised page are empty values with no diagnostics."""

    def test_no_air_date_label(self, fetch, shinden_html, diag_events):
        html = re.sub(r"<dt>Emisja</dt><dd>[^<]*</dd>", "", shinden_html("series_frieren.html"))
        meta = fetch(html)
        assert (meta.air_date, meta.air_date_sort) == (None, None)
        assert meta.tags and meta.related
        assert diag_events == []

    def test_no_description(self, fetch, shinden_html, diag_events):
        html = re.sub(r'<div id="description".*?</div>', "", shinden_html("series_frieren.html"), flags=re.S)
        assert fetch(html).description == ""
        assert diag_events == []

    def test_no_tags_at_all(self, fetch, shinden_html, diag_events):
        html = re.sub(r'<ul class="sh-tp-tags".*?</details>', "", shinden_html("series_frieren.html"), flags=re.S)
        assert fetch(html).tags == ()
        assert diag_events == []

    def test_no_related_section(self, fetch, shinden_html, diag_events):
        assert fetch(_without_rels(shinden_html("series_frieren.html"))).related == ()
        assert diag_events == []

    def test_related_section_with_empty_tile_list(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html")
        section = _rels_section(html)
        empty = re.sub(r"<li class=\"sh-tp-tile\">.*?</li>", "", section, flags=re.S)
        assert fetch(html.replace(section, empty)).related == ()
        assert diag_events == []

    def test_only_manga_related(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html").replace('href="/series/', 'href="/manga/')
        assert fetch(html).related == ()
        assert diag_events == []


@pytest.mark.unit
class TestSectionDrift:
    """A drifted optional section is reported on its own and never voids the other sections."""

    def test_invalid_date_keeps_other_sections(self, fetch, shinden_html, diag_events):
        meta = fetch(_emisja(shinden_html("series_frieren.html"), "31.02.2023"))
        assert meta.air_date is None
        assert len(meta.tags) == 15 and len(meta.related) == 4 and meta.description
        [event] = _events(diag_events, "shinden_parse_partial")
        assert event["page"] == "metadata:air_date"

    def test_renamed_tag_groups_with_highlights_present(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html").replace("sh-tp-taggroup", "sh-tp-tagset")
        meta = fetch(html)
        assert meta.tags == ()
        assert meta.related and meta.air_date_sort
        [event] = _events(diag_events, "shinden_parse_partial")
        assert event["page"] == "metadata:tags"
        assert event["invalid"] == "groups_missing:1"

    def test_group_without_heading_is_partial(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html").replace("<h3>Gatunki</h3>", "", 1)
        tags = fetch(html).tags
        assert "Przygodowe" not in tags and "Shounen" in tags
        [event] = _events(diag_events, "shinden_parse_partial")
        assert (event["page"], event["valid"], event["invalid"]) == ("metadata:tags", 4, "no_label:1")

    def test_group_without_tags_is_partial(self, fetch, shinden_html, diag_events):
        html = re.sub(r"(<h3>Pierwowzór</h3>\s*<ul>).*?(</ul>)", r"\1\2", shinden_html("series_frieren.html"), flags=re.S)
        assert "Manga" not in fetch(html).tags
        [event] = _events(diag_events, "shinden_parse_partial")
        assert event["invalid"] == "no_tags:1"

    def test_unknown_group_is_appended_not_dropped(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html").replace("<h3>Gatunki</h3>", "<h3>Nowa grupa</h3>")
        tags = fetch(html).tags
        assert tags[-4:] == ("Przygodowe", "Dramat", "Magia", "Fantasy")
        assert diag_events == []

    def test_tag_counter_mismatch_is_reported(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html").replace("Wszystkie tagi (15)", "Wszystkie tagi (16)")
        assert len(fetch(html).tags) == 15
        [event] = _events(diag_events, "shinden_count_mismatch")
        assert (event["page"], event["expected"], event["actual"]) == ("metadata:tags", 16, 15)

    def test_unknown_route_is_reported_but_valid_tiles_stay(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html").replace('href="/series/67494-haru-2024"', 'href="/anime/67494-haru-2024"')
        assert [r.id for r in fetch(html).related] == ["68641", "66087", "66033"]
        [event] = _events(diag_events, "shinden_parse_partial")
        assert (event["page"], event["invalid"], event["excluded"]) == ("metadata:related", "unknown_route:1", "not_anime:1")

    def test_all_tiles_broken_drops_related_only(self, fetch, shinden_html, diag_events):
        html = re.sub(r'class="sh-okl" href="[^"]*"', 'class="sh-okl"', shinden_html("series_frieren.html"))
        meta = fetch(html)
        assert meta.related == ()
        assert len(meta.tags) == 15 and meta.air_date_sort == (2023, 9, 29)
        [event] = _events(diag_events, "shinden_parse_partial")
        assert (event["page"], event["valid"], event["invalid"]) == ("metadata:related", 0, "bad_href:5")

    def test_unrecognised_items_in_related_section(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_frieren.html").replace('class="sh-tp-tile"', 'class="sh-tp-kafel"')
        assert fetch(html).related == ()
        [event] = _events(diag_events, "shinden_parse_partial")
        assert (event["page"], event["invalid"]) == ("metadata:related", "unrecognised_items:1")

    def test_related_counter_mismatch_is_reported(self, fetch, shinden_html, diag_events):
        html = shinden_html("series_titles_motywy.html").replace("Pokaż wszystkie (7)", "Pokaż wszystkie (89)")
        assert len(fetch(html).related) == 4
        [event] = _events(diag_events, "shinden_count_mismatch")
        assert (event["page"], event["expected"], event["actual"]) == ("metadata:related", 89, 7)

    def test_valid_tags_do_not_hide_related_drift(self, fetch, shinden_html, diag_events):
        html = re.sub(r'class="sh-okl" href="[^"]*"', 'class="sh-okl"', shinden_html("series_frieren.html"))
        fetch(html)
        pages = {e["page"] for e in _events(diag_events, "shinden_parse_partial")}
        assert pages == {"metadata:related"}


@pytest.mark.unit
class TestTagGroupOrderAnchor:
    def test_contains_all_expected_polish_group_names(self):
        """Anchor: group names are HTML parsing tokens matched against Shinden's h3 text, not UI strings."""
        assert _TAG_GROUP_ORDER == (
            "Gatunki",
            "Grupy docelowe",
            "Motywy",
            "Rodzaje postaci",
            "Miejsce i czas",
            "Pierwowzór",
        )
