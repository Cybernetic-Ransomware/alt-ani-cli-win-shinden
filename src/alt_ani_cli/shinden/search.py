import re

from curl_cffi import requests as cffi_requests
from selectolax.parser import HTMLParser

from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.models import SeriesHit
from alt_ani_cli.shinden.utils import _normalize_title

_SERIES_RE = re.compile(r"/series/(\d+)-([^/?#\s]+)")


def search_series(client: cffi_requests.Session, query: str) -> list[SeriesHit]:
    resp = client.get(f"{SHINDEN_BASE}/series", params={"search": query})
    resp.raise_for_status()
    return _parse_results(resp.text)


def _parse_results(html: str) -> list[SeriesHit]:
    """Parse shinden search results page.

    Results are rendered as <ul class="div-row"> elements — one per anime.
    The first <a href="/series/{id}-{slug}"> in each row is the title link;
    subsequent links are genre tags (/series?genres[]=...) and are ignored by
    the _SERIES_RE pattern that requires the id-slug path segment.
    """
    tree = HTMLParser(html)
    results: list[SeriesHit] = []
    seen: set[str] = set()

    for row in tree.css("ul.div-row"):
        series_node = None
        for a in row.css("a[href]"):
            href = a.attributes.get("href") or ""
            if _SERIES_RE.search(href):
                series_node = a
                break
        if series_node is None:
            continue

        href = series_node.attributes.get("href") or ""
        m = _SERIES_RE.search(href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))

        title = _normalize_title(series_node.text(strip=True)) or m.group(2).replace("-", " ").title()
        slug = m.group(2).split("/")[0]

        type_node = row.css_first("li.type-col")
        series_type = type_node.text(strip=True) if type_node else ""

        results.append(
            SeriesHit(
                id=m.group(1),
                slug=slug,
                title=title,
                url=f"{SHINDEN_BASE}/series/{m.group(1)}-{slug}",
                series_type=series_type,
            )
        )

    return results
