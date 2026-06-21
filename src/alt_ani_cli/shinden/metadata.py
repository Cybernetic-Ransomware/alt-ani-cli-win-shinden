import re

from curl_cffi import requests as cffi_requests
from selectolax.parser import HTMLParser

from alt_ani_cli.config import SHINDEN_BASE
from alt_ani_cli.models import RelatedSeries, SeriesMetadata, SeriesRef
from alt_ani_cli.shinden.utils import _check_age_gate, _normalize_title

_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_SERIES_RE = re.compile(r"/series/(\d+)-([^/?#\s]+)")

_TAG_GROUP_ORDER = (
    "Gatunki",
    "Grupy docelowe",
    "Pozostałe tagi",
    "Rodzaje postaci",
    "Miejsce i czas",
    "Pierwowzór",
)


def fetch_series_metadata(client: cffi_requests.Session, ref: SeriesRef) -> SeriesMetadata:
    resp = client.get(ref.url)
    resp.raise_for_status()
    _check_age_gate(resp.text)
    tree = HTMLParser(resp.text)
    air_date, air_sort = _parse_air_date(tree)
    return SeriesMetadata(
        air_date=air_date,
        air_date_sort=air_sort,
        description=_parse_description(tree),
        tags=_parse_tags(tree),
        related=_parse_related(tree),
    )


def _parse_air_date(tree: HTMLParser) -> tuple[str | None, tuple[int, int, int] | None]:
    try:
        for dt in tree.css("dt"):
            label = dt.text(strip=True).rstrip(":")
            if label == "Data emisji":
                dd = dt.next  # type: ignore[attr-defined]
                while dd is not None and dd.tag != "dd":
                    dd = dd.next  # type: ignore[attr-defined]
                if dd is not None:
                    raw = dd.text(strip=True)
                    m = _DATE_RE.search(raw)
                    if m:
                        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        return raw.strip(), (y, mo, d)
        # Fallback: scan all text for a date near "Data emisji"
        full = tree.root.text() if tree.root else ""
        idx = full.find("Data emisji")
        if idx != -1:
            snippet = full[idx : idx + 60]
            m = _DATE_RE.search(snippet)
            if m:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return m.group(0), (y, mo, d)
    except ValueError, AttributeError:
        pass
    return None, None


def _parse_description(tree: HTMLParser) -> str:
    node = tree.css_first("div#description") or tree.css_first(".title-full-description")
    if node is None:
        return ""
    paras = [p.text(strip=True) for p in node.css("p") if p.text(strip=True)]
    if paras:
        return "\n\n".join(paras)
    return node.text(strip=True)


def _parse_tags(tree: HTMLParser) -> tuple[str, ...]:
    table = tree.css_first("table.data-view-table")
    if table is None:
        return ()
    group_tags: dict[str, list[str]] = {g: [] for g in _TAG_GROUP_ORDER}
    for tr in table.css("tr"):
        tds = tr.css("td")
        if len(tds) < 2:
            continue
        label = tds[0].text(strip=True).rstrip(":")
        if label not in group_tags:
            continue
        for a in tds[1].css("ul.tags a"):
            text = a.text(strip=True)
            if text:
                group_tags[label].append(text)
    result: list[str] = []
    for group in _TAG_GROUP_ORDER:
        result.extend(group_tags[group])
    return tuple(result)


def _parse_related(tree: HTMLParser) -> tuple[RelatedSeries, ...]:
    section = None
    for box in tree.css("section.box"):
        h2 = box.css_first("h2.box-title")
        if h2 and "Powiązane" in h2.text(strip=True):
            section = box
            break
    if section is None:
        return ()
    related: list[RelatedSeries] = []
    for li in section.css("li.relation_t2t"):
        a = li.css_first("figcaption a[href]")
        if a is None:
            continue
        href = a.attributes.get("href") or ""
        m = _SERIES_RE.search(href)
        if not m:
            continue
        captions = li.css("figcaption.figure-type")
        kind = captions[0].text(strip=True) if len(captions) >= 1 else ""
        relation = captions[1].text(strip=True) if len(captions) >= 2 else "—"
        if kind != "Anime":
            continue
        title = _normalize_title(a.text(strip=True)) or m.group(2).replace("-", " ").title()
        slug = m.group(2)
        related.append(
            RelatedSeries(
                id=m.group(1),
                slug=slug,
                title=title,
                url=f"{SHINDEN_BASE}/series/{m.group(1)}-{slug}",
                relation=relation,
            )
        )
    return tuple(related)
