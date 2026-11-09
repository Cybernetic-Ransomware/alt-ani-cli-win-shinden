import math
import re
import time
from dataclasses import dataclass

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from selectolax.parser import HTMLParser

from alt_ani_cli import diagnostics
from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.errors import ParseError
from alt_ani_cli.models import EpisodeListing, EpisodeRow, SeriesRef
from alt_ani_cli.shinden.utils import ParseTally, _check_age_gate, _check_challenge, drift

# shinden h1.title contains a type label link (<a>Anime</a>) whose text
# is concatenated without a space into the title by selectolax .text().
# Strip any known type prefix that runs directly into the real title.
_TYPE_PREFIX_RE = re.compile(
    r"^(Anime|Manga|Manhua|Manhwa|Light\s+Novel|Visual\s+Novel|Novel|Music|ONA|OVA|Movie|Special)\s*",
    re.IGNORECASE,
)

_SERIES_URL_RE = re.compile(r"shinden\.pl/(series|titles)/(\d+)-([^/?#\s]+)")
_EPISODE_HREF_RE = re.compile(r"^(?:https?://shinden\.pl)?/episode/[^?#\s]+")
_PAGE_PARAM_RE = re.compile(r"[?&]strona=(\d+)")

EPISODES_PER_PAGE = 100
# 30 pages = 3000 episodes; anything longer is reported as incomplete instead of crawled further.
MAX_EPISODE_PAGES = 30
_PAGE_DELAY_SEC = 0.5
_PAGE = "episodes"


@dataclass(frozen=True)
class _EpisodePage:
    title: str
    rows: list[EpisodeRow]
    raw_rows: int
    total: int | None
    last_page: int


def parse_series_url(url: str) -> SeriesRef:
    m = _SERIES_URL_RE.search(url)
    if not m:
        raise ParseError(EXCEPTIONS["series"]["parse_error"].format(url=repr(url)))
    kind, series_id = m.group(1), m.group(2)
    slug = m.group(3).split("/")[0]
    return SeriesRef(
        id=series_id,
        slug=slug,
        title=slug.replace("-", " ").title(),
        url=f"{SHINDEN_BASE}/{kind}/{series_id}-{slug}",
    )


def _get_page(client: cffi_requests.Session, series_url: str, page: int) -> cffi_requests.Response:
    return client.get(f"{series_url}/episodes", params={"strona": page, "na_stronie": EPISODES_PER_PAGE})


def list_episodes(client: cffi_requests.Session, ref: SeriesRef) -> EpisodeListing:
    """Pages come newest-first, so a listing cut short lacks the oldest episodes; drift on any page raises."""
    resp = _get_page(client, ref.url, 1)
    if resp.status_code == 404 and "/series/" in ref.url:
        # A history entry may point at /series/ for a title that has since lost its online episodes.
        alt_url = ref.url.replace("/series/", "/titles/", 1)
        resp = _get_page(client, alt_url, 1)
        if resp.status_code != 404:
            ref = SeriesRef(id=ref.id, slug=ref.slug, title=ref.title, url=alt_url)
    resp.raise_for_status()

    tally = ParseTally(_PAGE)
    first = _parse_page(resp.text, tally)
    pages_total = first.last_page
    if first.total:
        by_counter = math.ceil(first.total / EPISODES_PER_PAGE)
        if by_counter != pages_total:
            diagnostics.shinden_count_mismatch(_PAGE, "pages", expected=by_counter, actual=pages_total)
            pages_total = max(pages_total, by_counter)

    rows = list(first.rows)
    raw_rows = first.raw_rows
    pages_fetched = 1
    reason: str | None = None
    for page_no in range(2, min(pages_total, MAX_EPISODE_PAGES) + 1):
        time.sleep(_PAGE_DELAY_SEC)
        try:
            page_resp = _get_page(client, ref.url, page_no)
            page_resp.raise_for_status()
        except CurlRequestException:
            reason = "http_error"
            break
        page = _parse_page(page_resp.text, tally)
        rows.extend(page.rows)
        raw_rows += page.raw_rows
        pages_fetched += 1
    if reason is None and pages_total > MAX_EPISODE_PAGES:
        reason = "page_limit"

    if reason is not None:
        diagnostics.shinden_episodes_incomplete(ref.id, pages_fetched, pages_total, reason)
    elif first.total is not None and raw_rows != first.total:
        diagnostics.shinden_count_mismatch(_PAGE, "total", expected=first.total, actual=raw_rows)

    episodes = _dedupe(rows, tally)
    tally.finish()
    return EpisodeListing(
        ref=SeriesRef(id=ref.id, slug=ref.slug, title=first.title or ref.title, url=ref.url),
        episodes=episodes,
        pages_fetched=pages_fetched,
        pages_total=pages_total,
        incomplete_reason=reason,
    )


def _dedupe(rows: list[EpisodeRow], tally: ParseTally) -> list[EpisodeRow]:
    seen_urls: set[str] = set()
    seen_numbers: set[float] = set()
    unique: list[EpisodeRow] = []
    for row in rows:
        if row.url in seen_urls or row.number in seen_numbers:
            tally.exclude("duplicate")
            continue
        seen_urls.add(row.url)
        seen_numbers.add(row.number)
        tally.accept()
        unique.append(row)
    return sorted(unique, key=lambda ep: ep.number)


def _parse_title(tree: HTMLParser) -> str:
    title = ""
    h1 = tree.css_first("h1.title") or tree.css_first("h1")
    if h1:
        title = h1.text(strip=True)
        title = _TYPE_PREFIX_RE.sub("", title).strip()
    if not title:
        title_tag = tree.css_first("title")
        if title_tag:
            raw = title_tag.text(strip=True)
            title = re.split(r" [-–|] ", raw)[0].strip()
    return title


def _parse_number(text: str) -> float | None:
    try:
        number = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _parse_page(html: str, tally: ParseTally) -> _EpisodePage:
    tree = HTMLParser(html)
    _check_challenge(tree, _PAGE)
    _check_age_gate(html)

    section = tree.css_first("section#odcinki")
    if section is None:
        raise drift(_PAGE, "episode list section#odcinki not found")
    count_node = section.css_first(".ep-list__count")
    total = _parse_number(count_node.text()) if count_node else None
    total_int = int(total) if total is not None else None

    rows = section.css("ol.ep-rows > li.ep-row")
    if not rows and total_int:
        raise drift(_PAGE, f"counter reports {total_int} episodes but the list has no rows")

    episodes: list[EpisodeRow] = []
    for row in rows:
        link = row.css_first("a.ep-row__link")
        href = (link.attributes.get("href") or "") if link else ""
        if not href:
            if row.css_first("span.ep-row__link") is not None:
                tally.exclude("unavailable")
            else:
                tally.reject("no_link")
            continue
        if not _EPISODE_HREF_RE.match(href):
            tally.reject("bad_href")
            continue
        no_node = row.css_first(".ep-row__no")
        number = _parse_number(no_node.text()) if no_node else None
        if number is None:
            tally.reject("bad_number")
            continue
        title_node = row.css_first(".ep-row__title")
        ep_title = (title_node.text(strip=True) if title_node else "") or f"Odcinek {number:g}"
        url = href if href.startswith("http") else SHINDEN_BASE + href
        episodes.append(EpisodeRow(number=number, title=ep_title, url=url))

    page_numbers = [
        int(p) for a in section.css("nav.ep-pages a[href]") for p in _PAGE_PARAM_RE.findall(a.attributes.get("href") or "")
    ]
    return _EpisodePage(
        title=_parse_title(tree),
        rows=episodes,
        raw_rows=len(rows),
        total=total_int,
        last_page=max(page_numbers, default=1),
    )
