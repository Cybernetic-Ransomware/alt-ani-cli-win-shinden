"""SQLite history: one short-lived connection per operation, so any thread may call it."""

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alt_ani_core.errors import HistoryVersionError
from alt_ani_core.history.store import DownloadEntry, LegacySeries, WatchEntry
from alt_ani_core.models import SeriesRef

SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5000

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE series (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            last_ep REAL,
            watch_updated_at TEXT,
            download_updated_at TEXT
        )""",
        """CREATE TABLE downloaded_episode (
            series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
            episode REAL NOT NULL,
            recorded_at TEXT,
            PRIMARY KEY (series_id, episode)
        )""",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteHistory:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._ready = False

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit: every transaction below is opened explicitly with BEGIN IMMEDIATE
        conn = sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._migrate(conn)
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            # only a committed transaction proves the schema exists
            self._ready = True

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            with self._write():
                pass
        with closing(self._connect()) as conn:
            yield conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Runs inside the caller's write transaction, so a failed migration leaves the file as it was."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise HistoryVersionError(f"history database schema {version} is newer than supported {SCHEMA_VERSION}").coded(
                "history.newer_schema", found=version, supported=SCHEMA_VERSION
            )
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS[step]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {step}")

    @staticmethod
    def _upsert_series(conn: sqlite3.Connection, series: SeriesRef) -> None:
        conn.execute(
            """INSERT INTO series (id, slug, title, url) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET slug = excluded.slug, title = excluded.title, url = excluded.url""",
            (series.id, series.slug, series.title, series.url),
        )

    def record_watch(self, series: SeriesRef, episode: float) -> None:
        with self._write() as conn:
            self._upsert_series(conn, series)
            conn.execute(
                "UPDATE series SET last_ep = ?, watch_updated_at = ? WHERE id = ?", (float(episode), _now(), series.id)
            )

    def record_download(self, series: SeriesRef, episode: float) -> None:
        now = _now()
        with self._write() as conn:
            self._upsert_series(conn, series)
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
        """Removes every entry but keeps ``meta``, so an already imported legacy file is not imported again."""
        with self._write() as conn:
            conn.execute("DELETE FROM series")

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
