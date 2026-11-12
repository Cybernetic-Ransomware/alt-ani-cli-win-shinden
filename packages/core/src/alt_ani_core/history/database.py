"""The local SQLite file: one short-lived connection per operation, schema upgrades behind a verified backup."""

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from alt_ani_core.errors import HistoryBackupError, HistoryVersionError
from alt_ani_core.history.migrations import MIGRATIONS, SCHEMA_VERSION
from alt_ani_core.models import EpisodeRow, SeriesRef

BUSY_TIMEOUT_MS = 5000
BACKUP_SUFFIX = ".bak"
_TEMP_SUFFIX = ".tmp"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def backup_glob(db_path: Path, version: int) -> str:
    return f"{db_path.name}.schema-{version}.*{BACKUP_SUFFIX}"


def _copy_database(source: Path, target: Path) -> None:
    """A second connection: the backup API stalls on the connection that holds the write lock."""
    with (
        closing(sqlite3.connect(source, timeout=BUSY_TIMEOUT_MS / 1000)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.execute("PRAGMA query_only = 1")
        src.backup(dst)


def _tables(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}  # nosec B608


def _verify_backup(locked: sqlite3.Connection, copy: Path, version: int) -> None:
    with closing(sqlite3.connect(copy)) as conn:
        conn.execute("PRAGMA query_only = 1")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("backup failed the integrity check")
        if conn.execute("PRAGMA user_version").fetchone()[0] != version:
            raise sqlite3.DatabaseError("backup has a different schema version")
        if _tables(conn) != _tables(locked):
            raise sqlite3.DatabaseError("backup does not hold the same tables and rows")


def _fsync(path: Path) -> None:
    with path.open("rb+") as fh:
        os.fsync(fh.fileno())


def _publish(temp: Path, final: Path) -> None:
    if final.exists():
        raise FileExistsError(final)
    os.replace(temp, final)
    if os.name == "posix":
        # the rename itself must survive a power loss, not only the file's content; Windows has no directory fsync
        fd = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Database:
    """Shared by every store of one file; any thread may call it, each call opens and closes its own connection."""

    def __init__(self, path: Path, *, clock: Callable[[], str] | None = None) -> None:
        self._path = path
        self._clock = clock or _now
        self._ready = False

    @property
    def path(self) -> Path:
        return self._path

    def now(self) -> str:
        return self._clock()

    def connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit: every transaction is opened explicitly with BEGIN IMMEDIATE
        conn = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def write(self, connect: Callable[[], sqlite3.Connection] | None = None) -> Iterator[sqlite3.Connection]:
        with closing((connect or self.connect)()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._migrate(conn)
                yield conn
            except BaseException:
                # SQLite rolls back by itself on some errors (a full disk); a second ROLLBACK would hide the real one
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            # only a committed transaction proves the schema exists
            self._ready = True

    @contextmanager
    def read(self, connect: Callable[[], sqlite3.Connection] | None = None) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            with self.write(connect):
                pass
        with closing((connect or self.connect)()) as conn:
            yield conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Runs inside the caller's write transaction, so a failed upgrade leaves the file as it was."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise HistoryVersionError(f"history database schema {version} is newer than supported {SCHEMA_VERSION}").coded(
                "history.newer_schema", found=version, supported=SCHEMA_VERSION
            )
        if version == SCHEMA_VERSION:
            return
        if version > 0:
            self._backup(conn, version)
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[step]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {step}")

    def _backup(self, locked: sqlite3.Connection, version: int) -> None:
        """Called while ``locked`` holds the write lock and has written nothing, so the copy is the pre-upgrade state."""
        # only the lock holder creates temporaries, so any left here belong to an interrupted earlier attempt
        for leftover in self._path.parent.glob(backup_glob(self._path, version) + _TEMP_SUFFIX + "*"):
            with suppress(OSError):
                leftover.unlink()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        final = self._path.with_name(f"{self._path.name}.schema-{version}.{stamp}{BACKUP_SUFFIX}")
        temp = final.with_name(final.name + _TEMP_SUFFIX)
        try:
            _copy_database(self._path, temp)
            _verify_backup(locked, temp, version)
            _fsync(temp)
            _publish(temp, final)
        except (OSError, sqlite3.Error) as exc:
            for path in (temp, temp.with_name(temp.name + "-journal")):
                with suppress(OSError):
                    path.unlink()
            raise HistoryBackupError(f"could not back up history schema {version} before upgrading: {exc}").coded(
                "history.backup_failed", version=version
            ) from None


def upsert_series(conn: sqlite3.Connection, series: SeriesRef) -> None:
    conn.execute(
        """INSERT INTO series (id, slug, title, url) VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET slug = excluded.slug, title = excluded.title, url = excluded.url""",
        (series.id, series.slug, series.title, series.url),
    )


def upsert_episode(conn: sqlite3.Connection, series: SeriesRef, episode: EpisodeRow, now: str) -> None:
    upsert_series(conn, series)
    conn.execute(
        """INSERT INTO episode (series_id, number, title, url, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(series_id, number) DO UPDATE SET
             title = excluded.title, url = excluded.url, updated_at = excluded.updated_at""",
        (series.id, float(episode.number), episode.title, episode.url, now),
    )
