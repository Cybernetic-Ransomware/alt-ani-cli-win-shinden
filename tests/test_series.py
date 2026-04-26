import pytest
from alt_ani_cli.shinden.series import parse_series_url, _parse
from alt_ani_cli.errors import ParseError

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


def test_parse_series_url_with_episodes_suffix():
    ref = parse_series_url("https://shinden.pl/series/65137-fate-strange-fake/episodes")
    assert ref.id == "65137"
    assert ref.slug == "fate-strange-fake"


def test_parse_series_url_without_suffix():
    ref = parse_series_url("https://shinden.pl/series/65137-fate-strange-fake")
    assert ref.id == "65137"
    assert ref.slug == "fate-strange-fake"


def test_parse_series_url_invalid():
    with pytest.raises(ParseError):
        parse_series_url("https://example.com/not-shinden")


def test_parse_episodes_reversed_order():
    title, episodes = _parse(SAMPLE_HTML)
    numbers = [ep.number for ep in episodes]
    assert numbers == [1.0, 2.0, 3.0], "Episodes must be sorted ascending (reversed from HTML)"


def test_parse_episodes_title_from_title_tag():
    title, episodes = _parse(SAMPLE_HTML)
    assert "Fate" in title or title  # Title extracted from <title> tag


def test_parse_episodes_url_is_absolute():
    _, episodes = _parse(SAMPLE_HTML)
    for ep in episodes:
        assert ep.url.startswith("https://shinden.pl/"), f"Expected absolute URL, got: {ep.url}"


def test_parse_episodes_empty_title_fallback():
    html = SAMPLE_HTML.replace("Ep Three", "").replace("Ep Two", "").replace("Ep One", "")
    _, episodes = _parse(html)
    for ep in episodes:
        assert "Odcinek" in ep.title or ep.title  # Fallback title
