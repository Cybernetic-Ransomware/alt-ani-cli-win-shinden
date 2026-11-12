"""Delete, verify and export library files; every step is idempotent and store-guarded, so no ownership is needed."""

from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.errors import (
    InvalidTransitionError,
    OperationCancelled,
    StateConflictError,
    StorageError,
    UnknownRecordError,
)
from alt_ani_core.history.library import SqliteLibraryStore
from alt_ani_core.history.ownership import DELETE_LOCK_SUFFIX, exclusive, lock_path
from alt_ani_core.state import ItemStatus, OfflineItem
from alt_ani_core.storage.api import FileStore
from alt_ani_core.storage.fs import export_copy


class ResultKind(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ItemResult:
    item_id: str
    kind: ResultKind
    code: str | None = None
    path: Path | None = None


class LibraryService:
    def __init__(self, library: SqliteLibraryStore, store: FileStore) -> None:
        self._library = library
        self._store = store

    def delete(self, item_ids: list[str], *, cancel: CancelToken = NEVER) -> list[ItemResult]:
        """One item at a time, so a refused or failing item does not stop the rest."""
        results: list[ItemResult] = []
        for item_id in item_ids:
            try:
                (item,) = self._library.mark_deleting([item_id])
            except (StateConflictError, InvalidTransitionError, UnknownRecordError) as exc:
                results.append(ItemResult(item_id, ResultKind.FAILED, exc.code))
                continue
            results.append(self.finish_delete(item, cancel=cancel))
        return results

    def finish_delete(self, item: OfflineItem, *, cancel: CancelToken = NEVER) -> ItemResult:
        """One cross-process lock spans check, removal and ``forget``; the row is re-read under it, not taken from ``item``."""
        try:
            with exclusive(lock_path(self._library.db_path, DELETE_LOCK_SUFFIX), cancel):
                return self._finish_delete(item.id, cancel)
        except OperationCancelled:
            return ItemResult(item.id, ResultKind.FAILED, "cancelled")

    def _finish_delete(self, item_id: str, cancel: CancelToken) -> ItemResult:
        try:
            item = self._library.get(item_id)
        except UnknownRecordError:
            return ItemResult(item_id, ResultKind.SKIPPED, "not_deleting")
        if item.status is not ItemStatus.DELETING:
            return ItemResult(item.id, ResultKind.SKIPPED, "not_deleting")
        try:
            kept = not self._is_recorded_copy(item, cancel)
            if not kept:
                self._store.remove(item.spec.locator)
        except StorageError as exc:
            # the row stays deleting: its locator is what a retry needs
            return ItemResult(item.id, ResultKind.FAILED, exc.code)
        with suppress(UnknownRecordError):
            self._library.forget(item.id)
        return ItemResult(item.id, ResultKind.SKIPPED if kept else ResultKind.OK, "foreign_file_kept" if kept else None)

    def _is_recorded_copy(self, item: OfflineItem, cancel: CancelToken) -> bool:
        stat = self._store.stat(item.spec.locator)
        if stat is None:
            return True
        if stat.is_dir or (item.spec.size_bytes is not None and stat.size != item.spec.size_bytes):
            return False
        if item.spec.sha256 is None:
            return True
        return self._store.digest(item.spec.locator, cancel) == item.spec.sha256

    def retry_pending_deletes(self, *, cancel: CancelToken = NEVER) -> list[ItemResult]:
        return [self.finish_delete(item, cancel=cancel) for item in self._library.items(status=ItemStatus.DELETING)]

    def verify(self, item_ids: list[str] | None = None, *, deep: bool = False, cancel: CancelToken = NEVER) -> list[ItemResult]:
        """Existence and size decide availability; ``deep`` also compares the SHA-256, both ways."""
        items = self._library.items() if item_ids is None else [self._library.get(i) for i in item_ids]
        results: list[ItemResult] = []
        for item in items:
            try:
                results.append(self._verify(item, deep=deep, cancel=cancel))
            except StorageError as exc:
                results.append(ItemResult(item.id, ResultKind.FAILED, exc.code))
        return results

    def _verify(self, item: OfflineItem, *, deep: bool, cancel: CancelToken) -> ItemResult:
        stat = self._store.stat(item.spec.locator)
        size_ok = stat is not None and not stat.is_dir and item.spec.size_bytes in (None, stat.size)
        if item.status is ItemStatus.AVAILABLE:
            if not size_ok:
                self._library.mark_missing(item.id)
                return ItemResult(item.id, ResultKind.FAILED, "missing")
            if deep and item.spec.sha256 and self._store.digest(item.spec.locator, cancel) != item.spec.sha256:
                # same length, other content: not the recorded copy, so it is no longer offered for playback
                self._library.mark_missing(item.id)
                return ItemResult(item.id, ResultKind.FAILED, "identity_mismatch")
            return ItemResult(item.id, ResultKind.OK)
        if item.status is ItemStatus.MISSING and deep and size_ok and stat is not None and item.spec.sha256:
            digest = self._store.digest(item.spec.locator, cancel)
            if digest == item.spec.sha256:
                self._library.mark_available(item.id, stat.size, digest)
                return ItemResult(item.id, ResultKind.OK, "restored")
            return ItemResult(item.id, ResultKind.FAILED, "identity_mismatch")
        return ItemResult(item.id, ResultKind.SKIPPED, item.status.value)

    def export(
        self, item_ids: list[str], dest_dir: Path, *, rename_on_conflict: bool = True, cancel: CancelToken = NEVER
    ) -> list[ItemResult]:
        results: list[ItemResult] = []
        for item_id in item_ids:
            try:
                item = self._library.get(item_id)
            except UnknownRecordError as exc:
                results.append(ItemResult(item_id, ResultKind.FAILED, exc.code))
                continue
            if item.status is not ItemStatus.AVAILABLE:
                results.append(ItemResult(item_id, ResultKind.SKIPPED, item.status.value))
                continue
            name = PurePosixPath(item.spec.locator.key).name or f"{item.id}.bin"
            try:
                with self._store.open_read(item.spec.locator) as source:
                    path = export_copy(source, dest_dir, name, cancel=cancel, rename_on_conflict=rename_on_conflict)
            except StorageError as exc:
                results.append(ItemResult(item_id, ResultKind.FAILED, exc.code))
                continue
            except OperationCancelled:
                results.append(ItemResult(item_id, ResultKind.FAILED, "cancelled"))
                break
            results.append(ItemResult(item_id, ResultKind.OK, path=path))
        return results
