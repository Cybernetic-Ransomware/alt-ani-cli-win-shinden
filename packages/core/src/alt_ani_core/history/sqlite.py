"""SQLite history: the CLI's resume cursor and download records, on the shared local database."""

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alt_ani_core.history.database import Database, upsert_series
from alt_ani_core.history.migrations import SCHEMA_VERSION
from alt_ani_core.history.store import DownloadEntry, LegacySeries, WatchEntry
from alt_ani_core.models import SeriesRef

__all__ = ["SCHEMA_VERSION", "SqliteHistory"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteHistory:
    def __init__(self, source: Path | Database) -> None:
        self._db = source if isinstance(source, Database) else Database(source)

    @property
    def path(self) -> Path:
        return self._db.path

    def _connect(self) -> sqlite3.Connection:
        return self._db.connect()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._db.write(self._connect) as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._db.read(self._connect) as conn:
            yield conn

    def record_watch(self, series: SeriesRef, episode: float) -> None:
        with self._write() as conn:
            upsert_series(conn, series)
            conn.execute(
                "UPDATE series SET last_ep = ?, watch_updated_at = ? WHERE id = ?", (float(episode), _now(), series.id)
            )

    def record_download(self, series: SeriesRef, episode: float) -> None:
        now = _now()
        with self._write() as conn:
            upsert_series(conn, series)
            conn.execute(
                "INSERT OR IGNORE INTO downloaded_episode (series_id, episode, recorded_at) VALUES (?, ?, ?)",
                (series.id, float(episode), now),
            )
            conn.execute("UPDATE series SET download_updated_at = ? WHERE id = ?", (now, series.id))

    def watch_entries(self) -> list[WatchEntry]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT id, slug, title, url, last_ep, COALESCE(watch_updated_at, '') FROM series
                   WHERE last_ep IS NOT NULL ORDER BY COALESCE(watch_updated_at, '') DESC, rowid DESC"""
            ).fetchall()
        return [WatchEntry(SeriesRef(id=r[0], slug=r[1], title=r[2], url=r[3]), float(r[4]), r[5]) for r in rows]

    def download_entries(self) -> list[DownloadEntry]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT s.id, s.slug, s.title, s.url, COALESCE(s.download_updated_at, ''), GROUP_CONCAT(d.episode)
                   FROM series s JOIN downloaded_episode d ON d.series_id = s.id
                   GROUP BY s.id ORDER BY COALESCE(s.download_updated_at, '') DESC, s.rowid DESC"""
            ).fetchall()
        return [
            DownloadEntry(
                SeriesRef(id=r[0], slug=r[1], title=r[2], url=r[3]),
                frozenset(float(n) for n in str(r[5]).split(",")),
                r[4],
            )
            for r in rows
        ]

    def clear(self) -> None:
        """Removes the cursor and download records only; ``meta``, watch state, jobs and the library stay."""
        with self._write() as conn:
            conn.execute("UPDATE series SET last_ep = NULL, watch_updated_at = NULL, download_updated_at = NULL")
            conn.execute("DELETE FROM downloaded_episode")
            # series rows still referenced by newer state must stay; the rest go, as before
            conn.execute("DELETE FROM series WHERE id NOT IN (SELECT series_id FROM episode)")

    def meta(self, key: str) -> str | None:
        with self._read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, values: dict[str, str]) -> None:
        with self._write() as conn:
            self._set_meta(conn, values)

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )

    def import_legacy(self, records: Iterable[LegacySeries], meta: dict[str, str]) -> None:
        """One transaction: existing rows win on watch fields, downloaded episodes are merged, ``meta`` marks it done."""
        with self._write() as conn:
            for rec in records:
                s = rec.series
                conn.execute(
                    """INSERT OR IGNORE INTO series (id, slug, title, url, last_ep, watch_updated_at, download_updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (s.id, s.slug, s.title, s.url, rec.last_ep, rec.watch_updated_at, rec.download_updated_at),
                )
                conn.execute(
                    """UPDATE series SET
                         last_ep = COALESCE(last_ep, ?),
                         watch_updated_at = CASE WHEN last_ep IS NULL THEN ? ELSE watch_updated_at END,
                         download_updated_at = COALESCE(download_updated_at, ?)
                       WHERE id = ?""",
                    (rec.last_ep, rec.watch_updated_at, rec.download_updated_at, s.id),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO downloaded_episode (series_id, episode, recorded_at) VALUES (?, ?, NULL)",
                    [(s.id, float(n)) for n in rec.downloaded],
                )
            self._set_meta(conn, meta)
