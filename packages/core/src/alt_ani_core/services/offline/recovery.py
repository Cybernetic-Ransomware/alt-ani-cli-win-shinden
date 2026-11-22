"""Start-up reconciliation after ownership and before any claim; idempotent, and only matching evidence completes."""

from dataclasses import dataclass, field
from pathlib import Path

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.errors import StorageError
from alt_ani_core.events import EventSink
from alt_ani_core.history.local import LocalState
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.rules.resume import evidence_of
from alt_ani_core.services.offline.cleanup import remove_pending
from alt_ani_core.services.offline.events import OrphanFound
from alt_ani_core.services.offline.library import LibraryService, ResultKind
from alt_ani_core.state import (
    TERMINAL_STATES,
    Claim,
    JobError,
    OfflineItemSpec,
    StorageLocator,
)
from alt_ani_core.storage.api import FileStore
from alt_ani_core.storage.fs import sha256_path

_MEDIA_SKIP_SUFFIXES = (".part", ".ytdl", ".json", ".tmp")


@dataclass(frozen=True)
class RecoveryReport:
    requeued: tuple[str, ...] = ()
    settled: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    marked_missing: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()
    deletes_finished: tuple[str, ...] = ()
    removals: int = 0
    orphans: tuple[StorageLocator, ...] = field(default=())


def _matches(path: Path, evidence: tuple[int, str], cancel: CancelToken) -> bool:
    try:
        return path.is_file() and path.stat().st_size == evidence[0] and sha256_path(path, cancel) == evidence[1]
    except OSError:
        return False


def _work_media(store: FileStore, claim: Claim) -> Path | None:
    partial = claim.job.partial
    if partial is None:
        return None
    try:
        work = store.work_path(partial.locator)
    except StorageError:
        return None
    files = [p for p in work.glob("media.*") if p.is_file() and not p.name.endswith(_MEDIA_SKIP_SUFFIXES)]
    return files[0] if len(files) == 1 else None


def _reconcile(state: LocalState, store: FileStore, claim: Claim, cancel: CancelToken) -> bool:
    """True when the job could be completed from its evidence; otherwise it fails and its files stay untouched."""
    queue, job = state.queue, claim.job
    partial, target = job.partial, job.target
    evidence = evidence_of(partial.validator) if partial else None
    if partial is None or target is None or evidence is None:
        queue.fail(claim.attempt_id, JobError("recovery.evidence_missing"))
        return False
    stat = store.stat(target)
    if stat is not None:
        same = not stat.is_dir and stat.size == evidence[0] and store.digest(target, cancel) == evidence[1]
        if not same:
            queue.fail(claim.attempt_id, JobError("storage.location_conflict"))
            return False
    else:
        media = _work_media(store, claim)
        if media is None or not _matches(media, evidence, cancel):
            queue.fail(claim.attempt_id, JobError("recovery.evidence_missing"))
            return False
        try:
            store.publish(media, target)
        except StorageError as exc:
            queue.fail(claim.attempt_id, JobError(exc.code or "storage.failed"))
            return False
    spec = OfflineItemSpec(
        locator=target,
        size_bytes=evidence[0],
        audio=partial.source.lang_audio or None,
        subs=partial.source.lang_subs or None,
        height=partial.variant.height,
        variant_label=partial.variant.label or None,
        protocol=partial.variant.protocol,
        container=Path(target.key).suffix.lstrip(".") or None,
        source_player=partial.source.player or None,
        sha256=evidence[1],
    )
    return queue.complete(claim.attempt_id, spec) is not None


def _referenced(state: LocalState) -> set[StorageLocator]:
    known: set[StorageLocator] = {item.spec.locator for item in state.library.items()}
    known |= {r.locator for r in state.queue.pending_removals()}
    for job in state.queue.jobs():
        if job.partial is not None:
            known.add(job.partial.locator)
        if job.target is not None and job.state not in TERMINAL_STATES:
            known.add(job.target)
    return known


def recover(
    state: LocalState, store: FileStore, owner: ExecutorOwnership, events: EventSink, cancel: CancelToken = NEVER
) -> RecoveryReport:
    recovery = state.queue.recover_interrupted(owner)
    completed: list[str] = []
    failed: list[str] = []
    for claim in recovery.reconcile:
        (completed if _reconcile(state, store, claim, cancel) else failed).append(claim.job.id)

    library = LibraryService(state.library, store)
    cancel.raise_if_cancelled()
    finished = [r.item_id for r in library.retry_pending_deletes(cancel=cancel) if r.kind is not ResultKind.FAILED]
    cancel.raise_if_cancelled()
    missing: list[str] = []
    restored: list[str] = []
    for result in library.verify(deep=True, cancel=cancel):
        if result.code == "missing":
            missing.append(result.item_id)
        elif result.code == "restored":
            restored.append(result.item_id)
    # the library steps report a cancelled item instead of raising; recovery itself must stop
    cancel.raise_if_cancelled()

    removals = remove_pending(state.queue, store)
    known = _referenced(state)
    orphans = tuple(loc for loc in store.scan() if loc not in known)
    for loc in orphans:
        events.emit(OrphanFound(backend=loc.backend, key=loc.key))
    return RecoveryReport(
        requeued=recovery.requeued,
        settled=recovery.settled,
        completed=tuple(completed),
        failed=tuple(failed),
        marked_missing=tuple(missing),
        restored=tuple(restored),
        deletes_finished=tuple(finished),
        removals=removals,
        orphans=orphans,
    )
