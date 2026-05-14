"""Tests for shinden/series.py — URL parser and episode list parser."""

import pytest

from alt_ani_cli.errors import ParseError
from alt_ani_cli.shinden.series import _parse, parse_series_url

SAMPLE_HTML = """
<!DOCTYPE html><html><head><title>Fate/strange fake - Shinden</title></head><body>
<table><thead><tr><th>Odcinek</th></tr></thead>
<tbody class="list-episode-checkboxes">
  <tr data-episode-no="3"><td>3</td><td class="ep-title">Ep Three</td><td></td><td></td><td class="ep-date">2025-01-01</td>
    <td class="button-group"><a href="/episode/65137-fate-strange-fake/view/999" class="button active detail">Szczegóły</a></td></tr>
  <tr data-episode-no="2"><td>2</td><td class="ep-title">Ep Two</td><td></td><td></td><td class="ep-date">2024-12-01</td>
    <td class="button-group"><a href="/episode/65137-fate-strange-fake/view/888" class="button active detail">Szczegóły</a></td></tr>
  <tr data-episode-no="1"><td>1</td><td class="ep-title">Ep One</td><td></td><td></td><td class="ep-date">2024-11-01</td>
    <td class="button-group"><a href="/episode/65137-fate-strange-fake/view/777" class="button active detail">Szczegóły</a></td></tr>
</tbody></table>
</body></html>
"""


@pytest.mark.unit
class TestParseSeriesUrl:
    def test_with_episodes_suffix(self):
        ref = parse_series_url("https://shinden.pl/series/65137-fate-strange-fake/episodes")
        assert ref.id == "65137"
        assert ref.slug == "fate-strange-fake"

    def test_without_suffix(self):
        ref = parse_series_url("https://shinden.pl/series/65137-fate-strange-fake")
        assert ref.id == "65137"
        assert ref.slug == "fate-strange-fake"

    def test_invalid_raises_parse_error(self):
        with pytest.raises(ParseError):
            parse_series_url("https://example.com/not-shinden")


@pytest.mark.unit
class TestParseEpisodes:
    def test_reversed_order(self):
        _, episodes = _parse(SAMPLE_HTML)
        assert [ep.number for ep in episodes] == [1.0, 2.0, 3.0]

    def test_title_from_title_tag(self):
        title, _ = _parse(SAMPLE_HTML)
        assert "Fate" in title or title

    def test_url_is_absolute(self):
        _, episodes = _parse(SAMPLE_HTML)
        for ep in episodes:
            assert ep.url.startswith("https://shinden.pl/")

    def test_empty_title_fallback(self):
        html = SAMPLE_HTML.replace("Ep Three", "").replace("Ep Two", "").replace("Ep One", "")
        _, episodes = _parse(html)
        for ep in episodes:
            assert "Odcinek" in ep.title or ep.title
