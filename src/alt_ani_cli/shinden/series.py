import re

import httpx
from selectolax.parser import HTMLParser

from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.errors import ParseError
from alt_ani_cli.shinden.models import EpisodeRow, SeriesRef

_SERIES_URL_RE = re.compile(r"shinden\.pl/series/(\d+)-([^/?#\s]+)")


def parse_series_url(url: str) -> SeriesRef:
    m = _SERIES_URL_RE.search(url)
    if not m:
        raise ParseError(f"Cannot extract series ID from URL: {url!r}")
    series_id = m.group(1)
    slug = m.group(2).split("/")[0]
    return SeriesRef(
        id=series_id,
        slug=slug,
        title=slug.replace("-", " ").title(),
        url=f"{SHINDEN_BASE}/series/{series_id}-{slug}",
    )


def list_episodes(client: httpx.Client, ref: SeriesRef) -> tuple[SeriesRef, list[EpisodeRow]]:
    """Returns (updated_ref_with_real_title, episodes_sorted_by_number)."""
    resp = client.get(f"{ref.url}/all-episodes")
    resp.raise_for_status()
    title, episodes = _parse(resp.text)

    if not episodes:
        resp2 = client.get(f"{ref.url}/episodes")
        resp2.raise_for_status()
        title, episodes = _parse(resp2.text)

    updated = SeriesRef(id=ref.id, slug=ref.slug, title=title or ref.title, url=ref.url)
    return updated, episodes


def _parse(html: str) -> tuple[str, list[EpisodeRow]]:
    tree = HTMLParser(html)

    title = ""
    h1 = tree.css_first("h1.title") or tree.css_first("h1")
    if h1:
        title = h1.text(strip=True)
    if not title:
        title_tag = tree.css_first("title")
        if title_tag:
            raw = title_tag.text(strip=True)
            title = raw.split(" - ")[0].split(" | ")[0].strip()

    rows = tree.css("tbody.list-episode-checkboxes tr")
    episodes: list[EpisodeRow] = []

    for row in rows:
        tds = row.css("td")
        if not tds:
            continue

        num_text = tds[0].text(strip=True)
        try:
            number = float(num_text)
        except ValueError:
            continue

        title_node = row.css_first("td.ep-title")
        ep_title = (title_node.text(strip=True) if title_node else "") or f"Odcinek {number:g}"

        link_node = (
            row.css_first("td.button-group a.button.active")
            or row.css_first("a[href*='/episode/']")
            or row.css_first("td.button-group a[href]")
        )
        if not link_node:
            continue

        href = link_node.attributes.get("href", "")
        if not href:
            continue
        if not href.startswith("http"):
            href = SHINDEN_BASE + href

        episodes.append(EpisodeRow(number=number, title=ep_title, url=href))

    episodes.reverse()
    return title, episodes
