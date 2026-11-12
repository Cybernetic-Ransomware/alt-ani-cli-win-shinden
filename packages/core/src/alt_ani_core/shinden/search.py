import re

from alt_ani_core import diagnostics
from alt_ani_core.config import SHINDEN_BASE
from alt_ani_core.dom import Document, Node
from alt_ani_core.dom import parse as parse_html
from alt_ani_core.models import SeriesHit
from alt_ani_core.shinden.utils import ParseTally, _check_challenge, drift
from alt_ani_core.transport.api import HttpSession

# /titles/ is the route for titles without online episodes; /series/ 404s for them.
_HIT_HREF_RE = re.compile(r"^(?:https?://shinden\.pl)?/(series|titles)/(\d+)-([^/?#\s]+)")
_PAGE = "search"


def search_series(client: HttpSession, query: str) -> list[SeriesHit]:
    resp = client.get(f"{SHINDEN_BASE}/series", params={"search": query})
    resp.raise_for_status()
    return _parse_results(resp.text)


def _total_count(tree: Document) -> int | None:
    """Result count across all pages, not just the rows rendered on this one."""
    node = tree.css_first("span.sh-tagi-liczba b")
    if node is None:
        return None
    digits = re.sub(r"\D", "", node.text(strip=True))
    return int(digits) if digits else None


def _text(row: Node, selector: str) -> str:
    node = row.css_first(selector)
    return " ".join(node.text().split()) if node else ""


def _parse_results(html: str) -> list[SeriesHit]:
    tree = parse_html(html)
    _check_challenge(tree, _PAGE)
    total = _total_count(tree)

    results_list = tree.css_first("ol.sh-tagi-wyniki")
    if results_list is None:
        if tree.css_first("div.sh-szukaj-brak") is not None and not total:
            return []
        raise drift(_PAGE, "results list ol.sh-tagi-wyniki not found")

    rows = results_list.css("li.sh-tagi-wynik")
    if not rows:
        if total == 0 or (total is None and tree.css_first("div.sh-szukaj-brak") is not None):
            return []
        detail = f"counter reports {total} results" if total else "result counter missing or unrecognised"
        raise drift(_PAGE, f"{detail} but the list has no rows")

    tally = ParseTally(_PAGE)
    results: list[SeriesHit] = []
    seen: set[str] = set()
    for row in rows:
        link = row.css_first("a[href]")
        m = _HIT_HREF_RE.match(link.attributes.get("href") or "") if link else None
        if m is None:
            tally.reject("bad_href")
            continue
        kind, series_id, slug = m.groups()
        if series_id in seen:
            tally.exclude("duplicate")
            continue
        seen.add(series_id)
        tally.accept()
        results.append(
            SeriesHit(
                id=series_id,
                slug=slug,
                title=_text(row, ".sh-tagi-nazwa") or slug.replace("-", " ").title(),
                url=f"{SHINDEN_BASE}/{kind}/{series_id}-{slug}",
                series_type=_text(row, ".sh-tagi-typ"),
            )
        )

    if total is not None and len(rows) > total:
        diagnostics.shinden_count_mismatch(_PAGE, "total", expected=total, actual=len(rows))
    tally.finish()
    return results
