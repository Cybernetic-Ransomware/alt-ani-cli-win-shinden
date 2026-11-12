"""Offline items: what is on the device, independent of watch history and of the job that produced it."""

import sqlite3

from alt_ani_core.errors import InvalidTransitionError, UnknownRecordError
from alt_ani_core.history.codec import item_of
from alt_ani_core.history.database import Database
from alt_ani_core.state import EpisodeKey, ItemStatus, OfflineItem


def _unknown(item_id: str) -> UnknownRecordError:
    return UnknownRecordError(f"no offline item {item_id}").coded("state.unknown_item", item_id=item_id)


class SqliteLibraryStore:
    """Rows only; files are removed and checked by the executor (stage 2), which then calls ``forget``/``mark_*``."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _get(conn: sqlite3.Connection, item_id: str) -> OfflineItem:
        row = conn.execute("SELECT * FROM offline_item WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise _unknown(item_id)
        return item_of(row)

    def get(self, item_id: str) -> OfflineItem:
        with self._db.read() as conn:
            return self._get(conn, item_id)

    def items(self, *, series_id: str | None = None, status: ItemStatus | None = None) -> list[OfflineItem]:
        sql = "SELECT * FROM offline_item WHERE (? IS NULL OR series_id = ?) AND (? IS NULL OR status = ?)"
        status_value = None if status is None else ItemStatus(status).value
        with self._db.read() as conn:
            rows = conn.execute(
                sql + " ORDER BY series_id, number, created_at", (series_id, series_id, status_value, status_value)
            ).fetchall()
        return [item_of(r) for r in rows]

    def for_episode(self, key: EpisodeKey) -> list[OfflineItem]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM offline_item WHERE series_id = ? AND number = ? ORDER BY created_at",
                (key.series_id, key.number),
            ).fetchall()
        return [item_of(r) for r in rows]

    def _move(self, item_id: str, allowed: tuple[ItemStatus, ...], target: ItemStatus, **extra: object) -> OfflineItem:
        with self._db.write() as conn:
            item = self._get(conn, item_id)
            if item.status is target:
                return item
            if item.status not in allowed:
                raise InvalidTransitionError(f"offline item {item_id} is {item.status}").coded(
                    "state.invalid_transition", item_id=item_id, state=item.status.value, requested=target.value
                )
            sets = ", ".join(f"{column} = ?" for column in extra)
            conn.execute(
                f"UPDATE offline_item SET status = ?, {sets + ', ' if sets else ''}revision = revision + 1 "  # nosec B608
                "WHERE id = ?",
                (target.value, *extra.values(), item_id),
            )
            return self._get(conn, item_id)

    def mark_deleting(self, item_ids: list[str]) -> list[OfflineItem]:
        """First step of a crash-safe delete: the row survives until the file is gone and ``forget`` is called."""
        return [self._move(i, (ItemStatus.AVAILABLE, ItemStatus.MISSING), ItemStatus.DELETING) for i in item_ids]

    def forget(self, item_id: str) -> None:
        """Removes a row marked deleting; watch state and the job's history stay, the job loses only the link."""
        with self._db.write() as conn:
            item = self._get(conn, item_id)
            if item.status is not ItemStatus.DELETING:
                raise InvalidTransitionError(f"offline item {item_id} is not being deleted").coded(
                    "state.invalid_transition", item_id=item_id, state=item.status.value, requested="forget"
                )
            conn.execute("DELETE FROM offline_item WHERE id = ?", (item_id,))

    def mark_missing(self, item_id: str) -> OfflineItem:
        return self._move(item_id, (ItemStatus.AVAILABLE,), ItemStatus.MISSING)

    def mark_available(self, item_id: str, size_bytes: int | None) -> OfflineItem:
        if size_bytes is not None and size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        return self._move(
            item_id, (ItemStatus.MISSING,), ItemStatus.AVAILABLE, size_bytes=size_bytes, verified_at=self._db.now()
        )
