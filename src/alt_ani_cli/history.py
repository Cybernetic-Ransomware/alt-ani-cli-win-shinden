import json
import os
from datetime import UTC, datetime

from alt_ani_cli.config import HISTORY_FILE, STATE_DIR
from alt_ani_cli.shinden.models import SeriesRef


def _load() -> dict:
    if not HISTORY_FILE.exists():
        return {"version": 1, "series": {}}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return {"version": 1, "series": {}}


def _save(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, HISTORY_FILE)


def upsert(series: SeriesRef, last_ep: float) -> None:
    data = _load()
    data["series"][series.id] = {
        "title": series.title,
        "slug": series.slug,
        "url": series.url,
        "last_ep": last_ep,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _save(data)


def list_all() -> list[tuple[SeriesRef, float]]:
    data = _load()
    result: list[tuple[SeriesRef, float]] = []
    for series_id, entry in data["series"].items():
        ref = SeriesRef(
            id=series_id,
            slug=entry.get("slug", ""),
            title=entry.get("title", series_id),
            url=entry.get("url", ""),
        )
        result.append((ref, float(entry.get("last_ep", 0))))
    result.sort(key=lambda x: x[0].title.lower())
    return result


def clear() -> None:
    _save({"version": 1, "series": {}})
