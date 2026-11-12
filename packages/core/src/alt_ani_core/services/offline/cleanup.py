"""Removes only what the store recorded as abandoned, in the transaction that abandoned it; nothing else is touched."""

from collections.abc import Iterable

from alt_ani_core.errors import StorageError
from alt_ani_core.history.queue import SqliteDownloadQueue
from alt_ani_core.state import StorageLocator
from alt_ani_core.storage.api import FileStore


def remove_pending(queue: SqliteDownloadQueue, store: FileStore, in_use: Iterable[StorageLocator] = ()) -> int:
    """Idempotent: a file already gone counts as removed; a failure keeps the record for the next pass."""
    busy = set(in_use)
    removed = 0
    for removal in queue.pending_removals():
        if removal.locator.backend != store.backend or removal.locator in busy:
            continue
        try:
            store.remove(removal.locator)
        except StorageError:
            continue
        queue.forget_removal(removal.id)
        removed += 1
    return removed
