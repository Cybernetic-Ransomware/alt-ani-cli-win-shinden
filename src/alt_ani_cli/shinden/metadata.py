from __future__ import annotations

import re

import httpx
from selectolax.parser import HTMLParser, Node

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


def fetch_series_metadata(client: httpx.Client, ref: SeriesRef) -> SeriesMetadata:
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
    try:
        for selector in (
            "section[itemprop='description']",
            "div[itemprop='description']",
            "section.description",
            "div.description",
            "article.description",
        ):
            node = tree.css_first(selector)
            if node is None:
                continue
            paras = node.css("p")
            if paras:
                return "\n\n".join(p.text(strip=True) for p in paras if p.text(strip=True))
            text = node.text(strip=True)
            if text:
                return text
    except AttributeError:
        pass
    return ""


def _parse_tags(tree: HTMLParser) -> tuple[str, ...]:
    try:
        group_tags: dict[str, list[str]] = {g: [] for g in _TAG_GROUP_ORDER}
        for dt in tree.css("dt"):
            label = dt.text(strip=True).rstrip(":")
            if label not in group_tags:
                continue
            dd = dt.next  # type: ignore[attr-defined]
            while dd is not None and dd.tag != "dd":
                dd = dd.next  # type: ignore[attr-defined]
            if dd is None:
                continue
            for a in dd.css("a"):
                text = a.text(strip=True)
                if text:
                    group_tags[label].append(text)
        result: list[str] = []
        for group in _TAG_GROUP_ORDER:
            result.extend(group_tags[group])
        return tuple(result)
    except Exception:
        return ()


def _parse_related(tree: HTMLParser) -> tuple[RelatedSeries, ...]:
    try:
        # Find the container near a dt with "Powiązane serie" label,
        # or a dedicated section.
        container: Node | None = None
        for dt in tree.css("dt"):
            if "Powiązane" in dt.text(strip=True):
                dd = dt.next  # type: ignore[attr-defined]
                while dd is not None and dd.tag != "dd":
                    dd = dd.next  # type: ignore[attr-defined]
                if dd is not None:
                    container = dd
                break
        if container is None:
            container = tree.css_first("section.related") or tree.css_first("div.related-series")

        if container is None:
            return ()

        related: list[RelatedSeries] = []
        # Each related entry is expected to have an <a href="/series/..."> and
        # sibling/child text nodes for type ("Anime"/"Manga") and relation ("Sequel" etc.).
        for a in container.css("a[href]"):
            href = a.attributes.get("href") or ""
            m = _SERIES_RE.search(href)
            if not m:
                continue
            # Walk the parent to find type and relation labels near this link.
            parent = a.parent
            if parent is None:
                continue
            parent_text = parent.text(strip=True)
            # Skip manga entries.
            if "Manga" in parent_text and "Anime" not in parent_text:
                continue
            # Require an "Anime" label somewhere in the parent block.
            if "Anime" not in parent_text:
                continue
            title = _normalize_title(a.text(strip=True)) or m.group(2).replace("-", " ").title()
            slug = m.group(2).split("/")[0]
            # Relation type: everything in parent text after stripping title and type label.
            relation = _extract_relation(parent_text, title)
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
    except Exception:
        return ()


def _extract_relation(parent_text: str, title: str) -> str:
    """Extract a relation label (Sequel, Prequel, etc.) from a block of text."""
    _KNOWN = ("Sequel", "Prequel", "Spin-off", "Adaptacja", "Inna wersja", "Alternatywna wersja", "Inne")
    for label in _KNOWN:
        if label in parent_text:
            return label
    # Fallback: strip the title and "Anime"/"Manga" noise and take the remainder.
    remainder = parent_text.replace(title, "").replace("Anime", "").replace("Manga", "").strip()
    return remainder or "—"
