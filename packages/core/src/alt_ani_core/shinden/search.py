import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from alt_ani_core import diagnostics
from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.config import SHINDEN_BASE
from alt_ani_core.dom import Document, Node
from alt_ani_core.dom import parse as parse_html
from alt_ani_core.models import SearchResults, SeriesHit
from alt_ani_core.shinden.utils import ParseTally, _check_challenge, drift
from alt_ani_core.transport.api import HttpSession, TransportError

# /titles/ is the route for titles without online episodes; /series/ 404s for them.
_HIT_HREF_RE = re.compile(r"^(?:https?://shinden\.pl)?/(series|titles)/(\d+)-([^/?#\s]+)")
_PAGE = "search"
# Shinden serves 48 hits per page; two pages keep the CLI's per-hit metadata prefetch under 100 requests.
MAX_SEARCH_PAGES = 2
_PAGE_DELAY_SEC = 0.5


@dataclass(frozen=True)
class _SearchPage:
    hits: list[SeriesHit]
    rows: int
    repeated: int
    total: int | None
    next_url: str | None


def search_series(client: HttpSession, query: str, *, cancel: CancelToken = NEVER) -> SearchResults:
    """Follows the site's "show more" link; a result cut short is marked incomplete, never passed off as whole."""
    resp = client.get(f"{SHINDEN_BASE}/series", params={"search": query})
    resp.raise_for_status()

    tally = ParseTally(_PAGE)
    seen: set[str] = set()
    first = _parse_page(resp.text, tally, seen, first=True)
    hits = list(first.hits)
    next_url = first.next_url
    pages_fetched = 1
    reason: str | None = None
    while next_url is not None:
        if pages_fetched >= MAX_SEARCH_PAGES:
            reason = "page_limit"
            break
        cancel.wait(_PAGE_DELAY_SEC)
        try:
            page_resp = client.get(next_url)
            page_resp.raise_for_status()
        except TransportError:
            reason = "http_error"
            break
        page = _parse_page(page_resp.text, tally, seen, first=False)
        pages_fetched += 1
        hits.extend(page.hits)
        if not page.rows:
            reason = "empty_page"
            break
        if page.repeated and not page.hits:
            reason = "repeated_page"
            break
        next_url = page.next_url

    if reason is None and first.total is not None and len(seen) != first.total:
        diagnostics.shinden_count_mismatch(_PAGE, "total", expected=first.total, actual=len(seen))
    if reason is not None:
        diagnostics.shinden_search_incomplete(pages_fetched, len(hits), first.total, reason)
    tally.finish()
    return SearchResults(hits=hits, total=first.total, pages_fetched=pages_fetched, incomplete_reason=reason)


def _total_count(tree: Document) -> int | None:
    """Result count across all pages, not just the rows rendered on this one."""
    node = tree.css_first("span.sh-tagi-liczba b")
    if node is None:
        return None
    digits = re.sub(r"\D", "", node.text(strip=True))
    return int(digits) if digits else None


def _next_url(tree: Document) -> str | None:
    link = tree.css_first("a[data-sh-wiecej]")
    if link is None:
        return None
    url = urljoin(f"{SHINDEN_BASE}/", link.attributes.get("href") or "")
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc, parts.path) != ("https", urlsplit(SHINDEN_BASE).netloc, "/series"):
        raise drift(_PAGE, "next-page link does not point at /series")
    return url


def _text(row: Node, selector: str) -> str:
    node = row.css_first(selector)
    return " ".join(node.text().split()) if node else ""


def _parse_page(html: str, tally: ParseTally, seen: set[str], *, first: bool) -> _SearchPage:
    """A page past the first may legitimately be empty while the counter stays positive (out-of-range page)."""
    tree = parse_html(html)
    _check_challenge(tree, _PAGE)
    total = _total_count(tree)
    empty_marker = tree.css_first("div.sh-szukaj-brak") is not None
    empty = _SearchPage(hits=[], rows=0, repeated=0, total=total, next_url=None)

    results_list = tree.css_first("ol.sh-tagi-wyniki")
    if results_list is None:
        if empty_marker and (not total or not first):
            return empty
        raise drift(_PAGE, "results list ol.sh-tagi-wyniki not found")

    rows = results_list.css("li.sh-tagi-wynik")
    if not rows:
        if total == 0 or (empty_marker and (total is None or not first)):
            return empty
        detail = f"counter reports {total} results" if total else "result counter missing or unrecognised"
        raise drift(_PAGE, f"{detail} but the list has no rows")

    hits: list[SeriesHit] = []
    repeated = 0
    for row in rows:
        link = row.css_first("a[href]")
        m = _HIT_HREF_RE.match(link.attributes.get("href") or "") if link else None
        if m is None:
            tally.reject("bad_href")
            continue
        kind, series_id, slug = m.groups()
        if series_id in seen:
            tally.exclude("duplicate")
            repeated += 1
            continue
        seen.add(series_id)
        tally.accept()
        hits.append(
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
    return _SearchPage(hits=hits, rows=len(rows), repeated=repeated, total=total, next_url=_next_url(tree))
