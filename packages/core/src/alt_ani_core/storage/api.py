"""What the offline executor needs from a file store; a platform adapter may have no plain paths for published files."""

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from alt_ani_core.cancel import CancelToken
from alt_ani_core.errors import (
    ACCESS_DENIED,
    FAILED,
    IN_USE,
    NO_ATOMIC_PUBLISH,
    NO_SPACE,
    OUTSIDE_ROOT,
    TARGET_EXISTS,
    UNAVAILABLE,
    StorageError,
    classify_os_error,
)
from alt_ani_core.state import StorageLocator

__all__ = [
    "ACCESS_DENIED",
    "FAILED",
    "IN_USE",
    "NO_ATOMIC_PUBLISH",
    "NO_SPACE",
    "OUTSIDE_ROOT",
    "TARGET_EXISTS",
    "UNAVAILABLE",
    "ExportTarget",
    "ExportWriter",
    "FileStat",
    "FileStore",
    "PlaybackTarget",
    "PublishName",
    "classify_os_error",
    "from_os_error",
    "storage_error",
]


@dataclass(frozen=True)
class FileStat:
    size: int
    is_dir: bool = False


@dataclass(frozen=True)
class PublishName:
    """What a published file is named after; never a URL or a host."""

    series_id: str
    series_title: str
    episode_label: str
    extension: str


@dataclass(frozen=True)
class PlaybackTarget:
    """What a player opens: ``file`` is a local path, ``content_uri`` a platform URI; never a remote URL."""

    kind: str
    value: str


class ExportWriter(Protocol):
    """One exported file: nothing is visible at the destination until ``commit``; ``abort`` removes what was written."""

    def write(self, chunk: bytes) -> None: ...

    def commit(self) -> str: ...

    def abort(self) -> None: ...


class ExportTarget(Protocol):
    """A destination outside the library, a directory or a platform picker; ``on_conflict`` is ``fail`` or ``rename``."""

    def create(self, name: str, *, on_conflict: str) -> ExportWriter: ...


def storage_error(code: str, **params: object) -> StorageError:
    return StorageError(code).coded(code, **params)


def from_os_error(exc: OSError) -> StorageError:
    return storage_error(classify_os_error(exc), errno=exc.errno)


class FileStore(Protocol):
    """Local work directories, opaque published locators; ``publish`` never replaces a file or fails ``check_root``."""

    @property
    def backend(self) -> str: ...

    def check_root(self) -> None: ...

    def partial_locator(self, job_id: str, partial_id: str) -> StorageLocator: ...

    def work_path(self, locator: StorageLocator) -> Path: ...

    def allocate(self, name: PublishName, exclude: frozenset[str] = frozenset()) -> StorageLocator: ...

    def publish(self, local: Path, target: StorageLocator) -> None: ...

    def stat(self, locator: StorageLocator) -> FileStat | None: ...

    def digest(self, locator: StorageLocator, cancel: CancelToken) -> str: ...

    def remove(self, locator: StorageLocator) -> bool: ...

    def open_read(self, locator: StorageLocator) -> BinaryIO: ...

    def playback_target(self, locator: StorageLocator) -> PlaybackTarget: ...

    def free_space(self) -> int | None: ...

    def scan(self) -> list[StorageLocator]: ...
