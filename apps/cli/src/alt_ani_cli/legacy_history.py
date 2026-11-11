"""One-way import of the pre-SQLite history.json; the source file is only ever read."""

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.history.store import LegacySeries
from alt_ani_core.models import SeriesRef

META_SHA = "legacy_json_sha"
META_STATUS = "legacy_json_status"
META_IMPORTED = "legacy_json_imported"
META_SKIPPED = "legacy_json_skipped"
BACKUP_SUFFIX = ".pre-sqlite.bak"


class ImportStatus(StrEnum):
    DONE = "done"
    # the file is readable but is not a history file; recorded with its hash, retried only once the file changes
    CORRUPT = "corrupt"
    # reading, backing up or writing failed; nothing is recorded, so the next start tries again
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ImportReport:
    status: ImportStatus
    imported: int = 0
    skipped: int = 0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _record(series_id: object, entry: object) -> LegacySeries | None:
    if not isinstance(series_id, str) or not series_id or not isinstance(entry, dict):
        return None
    last_ep = _number(entry.get("last_ep")) if "last_ep" in entry else None
    if "last_ep" in entry and last_ep is None:
        return None
    raw_downloaded = entry.get("downloaded_eps", [])
    if not isinstance(raw_downloaded, list):
        return None
    downloaded = [_number(n) for n in raw_downloaded]
    if any(n is None for n in downloaded):
        return None
    if last_ep is None and not downloaded:
        return None
    watch_at, download_at = entry.get("updated_at"), entry.get("download_updated_at")
    return LegacySeries(
        series=SeriesRef(
            id=series_id,
            slug=_text(entry.get("slug"), ""),
            title=_text(entry.get("title"), series_id),
            url=_text(entry.get("url"), ""),
        ),
        last_ep=last_ep,
        watch_updated_at=watch_at if isinstance(watch_at, str) else None,
        downloaded=tuple(n for n in downloaded if n is not None),
        download_updated_at=download_at if isinstance(download_at, str) else None,
    )


def _backup(source: Path, raw: bytes) -> None:
    backup = source.with_name(source.name + BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_bytes(raw)


def import_legacy(store: SqliteHistory, source: Path) -> ImportReport | None:
    """None when there is nothing to do: no legacy file, or this exact file content was already handled."""
    if not source.is_file():
        return None
    try:
        raw = source.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if store.meta(META_SHA) == sha:
            return None
        _backup(source, raw)
    except OSError, sqlite3.Error:
        return ImportReport(ImportStatus.UNAVAILABLE)

    try:
        data = json.loads(raw.decode("utf-8"))
        series = data["series"] if isinstance(data, dict) else None
    except ValueError, KeyError:
        series = None

    try:
        if not isinstance(series, dict):
            store.set_meta({META_SHA: sha, META_STATUS: ImportStatus.CORRUPT.value})
            return ImportReport(ImportStatus.CORRUPT)
        records = [record for sid, entry in series.items() if (record := _record(sid, entry)) is not None]
        skipped = len(series) - len(records)
        store.import_legacy(
            records,
            {
                META_SHA: sha,
                META_STATUS: ImportStatus.DONE.value,
                META_IMPORTED: str(len(records)),
                META_SKIPPED: str(skipped),
            },
        )
    except OSError, sqlite3.Error:
        return ImportReport(ImportStatus.UNAVAILABLE)
    return ImportReport(ImportStatus.DONE, imported=len(records), skipped=skipped)
