"""Every store of one local database file, sharing its connection and migration handling."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from alt_ani_core.history.database import Database
from alt_ani_core.history.library import SqliteLibraryStore
from alt_ani_core.history.preferences import SqlitePreferencesStore
from alt_ani_core.history.queue import SqliteDownloadQueue
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.history.watch import SqliteWatchStore


@dataclass(frozen=True)
class LocalState:
    database: Database
    history: SqliteHistory
    watch: SqliteWatchStore
    queue: SqliteDownloadQueue
    library: SqliteLibraryStore
    preferences: SqlitePreferencesStore


def open_local_state(path: Path, *, clock: Callable[[], str] | None = None) -> LocalState:
    """Touches no file until the first operation."""
    db = Database(path, clock=clock)
    return LocalState(
        database=db,
        history=SqliteHistory(db),
        watch=SqliteWatchStore(db),
        queue=SqliteDownloadQueue(db),
        library=SqliteLibraryStore(db),
        preferences=SqlitePreferencesStore(db),
    )
