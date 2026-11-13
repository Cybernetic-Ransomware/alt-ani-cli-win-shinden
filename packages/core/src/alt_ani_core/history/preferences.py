"""Global preferences shared by watching and downloading; jobs keep their own copy of the criteria."""

import dataclasses
import sqlite3
from typing import Any

from alt_ani_core.errors import StateConflictError
from alt_ani_core.history.database import Database
from alt_ani_core.state import DownloadNetwork, Preferences, QualityCriterion


def _read(conn: sqlite3.Connection) -> Preferences:
    row = conn.execute("SELECT * FROM preferences WHERE id = 1").fetchone()
    return Preferences(
        audio=row["audio"],
        subs=row["subs"],
        quality=QualityCriterion.parse(row["quality"]),
        download_network=DownloadNetwork(row["download_network"]),
        auto_alternatives=bool(row["auto_alternatives"]),
        max_active_downloads=row["max_active_downloads"],
        revision=row["revision"],
    )


class SqlitePreferencesStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> Preferences:
        with self._db.read() as conn:
            return _read(conn)

    def update(self, *, expected_revision: int | None = None, **changes: Any) -> Preferences:
        """Validates the whole result before writing; a stale ``expected_revision`` is a conflict, not a merge."""
        if "revision" in changes:
            raise TypeError("the revision is not a preference")
        with self._db.write() as conn:
            current = _read(conn)
            if expected_revision is not None and expected_revision != current.revision:
                raise StateConflictError("preferences changed since they were read").coded(
                    "state.conflict", expected=expected_revision, found=current.revision
                )
            new = dataclasses.replace(current, **changes, revision=current.revision + 1)
            conn.execute(
                """UPDATE preferences SET audio = ?, subs = ?, quality = ?, download_network = ?,
                     auto_alternatives = ?, max_active_downloads = ?, revision = ? WHERE id = 1""",
                (
                    new.audio,
                    new.subs,
                    new.quality.encode(),
                    DownloadNetwork(new.download_network).value,
                    int(new.auto_alternatives),
                    new.max_active_downloads,
                    new.revision,
                ),
            )
        return new
