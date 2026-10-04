import json
import os
from datetime import UTC, datetime

from alt_ani_cli.config import HISTORY_FILE, STATE_DIR
from alt_ani_cli.models import SeriesRef


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


def _merge_entry(data: dict, series: SeriesRef) -> dict:
    """Return the series entry with fresh metadata, keeping fields owned by the other history kind."""
    entry = data["series"].setdefault(series.id, {})
    entry.update(title=series.title, slug=series.slug, url=series.url)
    return entry


def _ref(series_id: str, entry: dict) -> SeriesRef:
    return SeriesRef(
        id=series_id,
        slug=entry.get("slug", ""),
        title=entry.get("title", series_id),
        url=entry.get("url", ""),
    )


def upsert(series: SeriesRef, last_ep: float) -> None:
    data = _load()
    entry = _merge_entry(data, series)
    entry["last_ep"] = last_ep
    entry["updated_at"] = datetime.now(UTC).isoformat()
    _save(data)


def record_download(series: SeriesRef, ep_number: float) -> None:
    data = _load()
    entry = _merge_entry(data, series)
    downloaded = {float(n) for n in entry.get("downloaded_eps", [])}
    downloaded.add(float(ep_number))
    entry["downloaded_eps"] = sorted(downloaded)
    entry["download_updated_at"] = datetime.now(UTC).isoformat()
    _save(data)


def list_all() -> list[tuple[SeriesRef, float]]:
    """Watch history only — download-only entries have no last_ep and are skipped."""
    data = _load()
    watched = [(sid, e) for sid, e in data["series"].items() if "last_ep" in e]
    watched.sort(key=lambda item: item[1].get("updated_at", ""), reverse=True)
    return [(_ref(sid, e), float(e["last_ep"])) for sid, e in watched]


def list_downloads() -> list[tuple[SeriesRef, frozenset[float]]]:
    data = _load()
    downloads = [(sid, e) for sid, e in data["series"].items() if e.get("downloaded_eps")]
    downloads.sort(key=lambda item: item[1].get("download_updated_at", ""), reverse=True)
    return [(_ref(sid, e), frozenset(float(n) for n in e["downloaded_eps"])) for sid, e in downloads]


def clear() -> None:
    _save({"version": 1, "series": {}})
