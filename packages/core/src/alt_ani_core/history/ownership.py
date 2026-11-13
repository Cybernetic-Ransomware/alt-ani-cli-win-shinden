"""Executor ownership of one database file: an OS lock the system drops when the owning process dies."""

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from alt_ani_core.cancel import NEVER, CancelToken

LOCK_SUFFIX = ".executor-lock"
DELETE_LOCK_SUFFIX = ".delete-lock"
_LOCK_POLL_SEC = 0.05

_registry_lock = threading.Lock()
_held: dict[Path, ExecutorOwnership] = {}


def lock_path(db_path: Path, suffix: str = LOCK_SUFFIX) -> Path:
    return db_path.with_name(db_path.name + suffix)


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    # flock, not fcntl/lockf: those are dropped when the process closes any descriptor of the file
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive(path: Path, cancel: CancelToken = NEVER) -> Iterator[None]:
    """Blocks until held; each holder opens its own descriptor, so threads of one process exclude each other too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while not _try_lock(fd):
            cancel.wait(_LOCK_POLL_SEC)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


class ExecutorOwnership:
    """Held by at most one process (and one object in it) per database; no lease, so a stalled owner keeps it."""

    def __init__(self, db_path: Path, fd: int) -> None:
        self._db_path = db_path
        self._fd: int | None = fd

    @classmethod
    def try_acquire(cls, db_path: Path) -> Self | None:
        resolved = db_path.resolve()
        with _registry_lock:
            if resolved in _held:
                return None
            resolved.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path(resolved), os.O_RDWR | os.O_CREAT, 0o600)
            if not _try_lock(fd):
                os.close(fd)
                return None
            owner = cls(resolved, fd)
            _held[resolved] = owner
            return owner

    @staticmethod
    def held_in_process(db_path: Path) -> bool:
        """Reads this process's registry only and takes no lock; another process's ownership is not visible here."""
        with _registry_lock:
            return db_path.resolve() in _held

    @property
    def db_path(self) -> Path:
        return self._db_path

    def held(self) -> bool:
        return self._fd is not None

    def owns(self, db_path: Path) -> bool:
        return self._fd is not None and db_path.resolve() == self._db_path

    def release(self) -> None:
        """Only once no thread of this process still does I/O for a claimed job."""
        with _registry_lock:
            if self._fd is None:
                return
            fd, self._fd = self._fd, None
            _held.pop(self._db_path, None)
            try:
                _unlock(fd)
            finally:
                os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
