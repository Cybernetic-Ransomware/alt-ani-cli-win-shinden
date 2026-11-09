import re
from contextlib import suppress
from datetime import date

from curl_cffi import requests as cffi_requests
from selectolax.parser import HTMLParser, Node

from alt_ani_cli import diagnostics
from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.errors import ParseError
from alt_ani_cli.models import RelatedSeries, SeriesMetadata, SeriesRef
from alt_ani_cli.shinden.utils import ParseTally, _check_age_gate, _check_challenge, drift

# DD.MM.YYYY, MM.YYYY (announcements) or YYYY; matched against the whole first token of the "Emisja" value.
_DATE_TOKEN_RE = re.compile(r"(?:(?:(\d{1,2})\.)?(\d{1,2})\.)?(\d{4})")
_TILE_HREF_RE = re.compile(r"^(?:https?://shinden\.pl)?/([a-z]+)/(\d+)-([^/?#\s]+)")
_SUMMARY_COUNT_RE = re.compile(r"\((\d+)\)")
_ANIME_ROUTES = ("series", "titles")
_NON_ANIME_ROUTES = ("manga", "books")
_PAGE = "metadata"

_TAG_GROUP_ORDER = (
    "Gatunki",
    "Grupy docelowe",
    "Motywy",
    "Rodzaje postaci",
    "Miejsce i czas",
    "Pierwowzór",
)


def fetch_series_metadata(client: cffi_requests.Session, ref: SeriesRef) -> SeriesMetadata:
    resp = client.get(ref.url)
    resp.raise_for_status()
    _check_age_gate(resp.text)
    tree = HTMLParser(resp.text)
    _check_challenge(tree, _PAGE)
    info = tree.css_first("dl.sh-tp-info")
    if info is None:
        raise drift(_PAGE, "info list dl.sh-tp-info not found")
    if not info.css("dt"):
        raise drift(_PAGE, "info list dl.sh-tp-info has no entries")
    air_date, air_sort = _parse_air_date(info)
    return SeriesMetadata(
        air_date=air_date,
        air_date_sort=air_sort,
        description=_parse_description(tree),
        tags=_parse_tags(tree),
        related=_parse_related(tree),
    )


def _report(tally: ParseTally) -> None:
    """Optional sections are tallied separately: a broken one is logged but never discards the rest of the page."""
    with suppress(ParseError):
        tally.finish()


def _summary_count(details: Node | None) -> int | None:
    """Counter in a "Wszystkie tagi (15)" / "Pokaż wszystkie (89)" disclosure summary."""
    summary = details.css_first("summary") if details else None
    m = _SUMMARY_COUNT_RE.search(summary.text()) if summary else None
    return int(m.group(1)) if m else None


def _check_count(section: str, counter: Node | None, actual: int) -> None:
    expected = _summary_count(counter)
    if expected is not None and expected != actual:
        diagnostics.shinden_count_mismatch(f"{_PAGE}:{section}", "summary", expected=expected, actual=actual)


def _text(node: Node, selector: str) -> str:
    found = node.css_first(selector)
    return " ".join(found.text().split()) if found else ""


def _info_value(info: Node, label: str) -> Node | None:
    for dt in info.css("dt"):
        if dt.text(strip=True).rstrip(":") != label:
            continue
        dd = dt.next
        while dd is not None and dd.tag != "dd":
            dd = dd.next
        return dd
    return None


def _parse_air_date(info: Node) -> tuple[str | None, tuple[int, int, int] | None]:
    """Only the start of a "DD.MM.YYYY – DD.MM.YYYY" range is kept; unknown parts of a partial date sort as 0."""
    dd = _info_value(info, "Emisja")
    if dd is None:
        return None, None
    raw = dd.text(strip=True)
    if not any(ch.isdigit() for ch in raw):
        return None, None
    tally = ParseTally(f"{_PAGE}:air_date")
    token = raw.split()[0]
    m = _DATE_TOKEN_RE.fullmatch(token)
    if m is None:
        tally.reject("bad_format")
        _report(tally)
        return None, None
    day, month, year = m.groups()
    try:
        date(int(year), int(month or 1), int(day or 1))
    except ValueError:
        tally.reject("bad_date")
        _report(tally)
        return None, None
    return token, (int(year), int(month or 0), int(day or 0))


def _collect_text(node: Node, paragraphs: list[list[str]]) -> None:
    for child in node.iter(include_text=True):
        if child.tag == "br":
            paragraphs.append([])
        elif child.tag == "-text":
            paragraphs[-1].append(child.text_content or "")
        else:
            _collect_text(child, paragraphs)


def _parse_description(tree: HTMLParser) -> str:
    node = tree.css_first("div#description")
    if node is None:
        return ""
    paragraphs: list[list[str]] = [[]]
    _collect_text(node, paragraphs)
    joined = (" ".join("".join(chunks).split()) for chunks in paragraphs)
    return "\n\n".join(p for p in joined if p)


def _parse_tags(tree: HTMLParser) -> tuple[str, ...]:
    tally = ParseTally(f"{_PAGE}:tags")
    groups: dict[str, list[str]] = {}
    links = 0
    for group in tree.css(".sh-tp-taggroup"):
        heading = group.css_first("h3")
        label = heading.text(strip=True) if heading else ""
        anchors = group.css("li a")
        links += len(anchors)
        names = [name for a in anchors if (name := a.text(strip=True))]
        if not label:
            tally.reject("no_label")
            continue
        if not names:
            tally.reject("no_tags")
            continue
        tally.accept()
        groups.setdefault(label, []).extend(names)
    if not tally.found and tree.css_first("ul.sh-tp-tags a") is not None:
        tally.reject("groups_missing")
    _check_count("tags", tree.css_first("details.sh-tp-tags-all"), links)
    _report(tally)
    ordered = [g for g in _TAG_GROUP_ORDER if g in groups] + [g for g in groups if g not in _TAG_GROUP_ORDER]
    return tuple(dict.fromkeys(tag for g in ordered for tag in groups[g]))


def _parse_related(tree: HTMLParser) -> tuple[RelatedSeries, ...]:
    """Anime is told apart by the link route, since the tile label is the format (TV, Film, Manga...), not the medium."""
    section = tree.css_first('section[aria-labelledby="sh-tp-h-rels"]')
    if section is None:
        return ()
    tally = ParseTally(f"{_PAGE}:related")
    # Long lists put the first row in ul.sh-tp-tiles--row and the rest behind details.sh-tp-more.
    tiles = section.css("li.sh-tp-tile")
    if not tiles and section.css_first("li, a[href]") is not None:
        tally.reject("unrecognised_items")
    _check_count("related", section.css_first("details.sh-tp-more"), len(tiles))
    related: list[RelatedSeries] = []
    seen: set[str] = set()
    for tile in tiles:
        link = tile.css_first("a[href]")
        m = _TILE_HREF_RE.match(link.attributes.get("href") or "") if link else None
        if m is None:
            tally.reject("bad_href")
            continue
        route, series_id, slug = m.groups()
        if route in _NON_ANIME_ROUTES:
            tally.exclude("not_anime")
            continue
        if route not in _ANIME_ROUTES:
            tally.reject("unknown_route")
            continue
        if series_id in seen:
            tally.exclude("duplicate")
            continue
        seen.add(series_id)
        tally.accept()
        related.append(
            RelatedSeries(
                id=series_id,
                slug=slug,
                title=_text(tile, ".sh-tp-tile__name") or slug.replace("-", " ").title(),
                url=f"{SHINDEN_BASE}/{route}/{series_id}-{slug}",
                relation=_text(tile, ".sh-tp-tile__rel") or "—",
            )
        )
    _report(tally)
    return tuple(related)
