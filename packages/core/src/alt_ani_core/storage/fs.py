"""Desktop file store: published files and job work areas under one managed root, keys are relative POSIX paths."""

import hashlib
import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.errors import StorageError
from alt_ani_core.state import StorageLocator
from alt_ani_core.storage import naming
from alt_ani_core.storage.api import (
    NO_ATOMIC_PUBLISH,
    OUTSIDE_ROOT,
    TARGET_EXISTS,
    UNAVAILABLE,
    ExportWriter,
    FileStat,
    PlaybackTarget,
    PublishName,
    from_os_error,
    storage_error,
)

BACKEND = "fs"
WORK_DIR = ".work"
EXPORT_PREFIX = ".alt-ani-export-"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CHUNK = 1024 * 1024
_MAX_ALLOCATE = 100


def fsync_file(path: Path) -> None:
    with path.open("rb+") as fh:
        os.fsync(fh.fileno())


def fsync_dir(path: Path) -> None:
    """Makes a rename or link in ``path`` survive a power loss; Windows has no directory fsync."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_of(fh: BinaryIO, cancel: CancelToken = NEVER) -> str:
    digest = hashlib.sha256()
    while chunk := fh.read(_CHUNK):
        cancel.raise_if_cancelled()
        digest.update(chunk)
    return digest.hexdigest()


def sha256_path(path: Path, cancel: CancelToken = NEVER) -> str:
    with path.open("rb") as fh:
        return sha256_of(fh, cancel)


def publish_no_replace(local: Path, dst: Path) -> None:
    """Never replaces an existing ``dst``: a hard link on POSIX (EEXIST is atomic), a plain rename on Windows."""
    try:
        fsync_file(local)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.link(local, dst)
            fsync_dir(dst.parent)
            os.unlink(local)
        else:
            # MoveFileEx without MOVEFILE_REPLACE_EXISTING refuses an existing destination
            os.rename(local, dst)
    except FileExistsError:
        raise storage_error(TARGET_EXISTS) from None
    except OSError as exc:
        raise from_os_error(exc) from None


class FsFileStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def backend(self) -> str:
        return BACKEND

    @property
    def root(self) -> Path:
        return self._root

    def check_root(self) -> None:
        """Creates the root; refuses a POSIX file system without hard links, where publishing could overwrite."""
        try:
            (self._root / WORK_DIR).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise storage_error(UNAVAILABLE, errno=exc.errno) from None
        if os.name != "posix":
            return
        probe = self._root / WORK_DIR / f".probe-{uuid.uuid4().hex}"
        linked = probe.with_name(probe.name + "-link")
        try:
            probe.write_bytes(b"")
            os.link(probe, linked)
        except OSError as exc:
            raise storage_error(NO_ATOMIC_PUBLISH, errno=exc.errno) from None
        finally:
            for path in (linked, probe):
                path.unlink(missing_ok=True)

    def _path(self, locator: StorageLocator) -> Path:
        if locator.backend != BACKEND:
            raise storage_error(OUTSIDE_ROOT, backend=locator.backend)
        key = locator.key.rstrip("/")
        posix, windows = PurePosixPath(key), PureWindowsPath(key)
        if posix.is_absolute() or windows.drive or windows.root or "\\" in key:
            raise storage_error(OUTSIDE_ROOT)
        if any(part in ("", ".", "..") for part in key.split("/")):
            raise storage_error(OUTSIDE_ROOT)
        path = self._root.joinpath(*posix.parts)
        root = os.path.realpath(self._root)
        if os.path.commonpath([root, os.path.realpath(path)]) != root:
            raise storage_error(OUTSIDE_ROOT)
        return path

    def partial_locator(self, job_id: str, partial_id: str) -> StorageLocator:
        if not (_ID_RE.fullmatch(job_id) and _ID_RE.fullmatch(partial_id)):
            raise storage_error(OUTSIDE_ROOT)
        return StorageLocator(BACKEND, f"{WORK_DIR}/{job_id}/{partial_id}/")

    def work_path(self, locator: StorageLocator) -> Path:
        """The local directory of a partial; created on first use, only inside the work area."""
        if not locator.key.startswith(f"{WORK_DIR}/"):
            raise storage_error(OUTSIDE_ROOT)
        path = self._path(locator)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise from_os_error(exc) from None
        return path

    def allocate(self, name: PublishName, exclude: frozenset[str] = frozenset()) -> StorageLocator:
        """Free on disk now and not in ``exclude``; the database reservation and the no-replace publish are the guards."""
        for attempt in range(1, _MAX_ALLOCATE + 1):
            key = naming.publish_key(name.series_id, name.series_title, name.episode_label, name.extension, attempt)
            locator = StorageLocator(BACKEND, key)
            if key not in exclude and not os.path.lexists(self._path(locator)):
                return locator
        raise storage_error(TARGET_EXISTS)

    def publish(self, local: Path, target: StorageLocator) -> None:
        publish_no_replace(local, self._path(target))

    def stat(self, locator: StorageLocator) -> FileStat | None:
        try:
            st = self._path(locator).stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise from_os_error(exc) from None
        return FileStat(size=st.st_size, is_dir=os.path.isdir(self._path(locator)))

    def digest(self, locator: StorageLocator, cancel: CancelToken = NEVER) -> str:
        try:
            return sha256_path(self._path(locator), cancel)
        except OSError as exc:
            raise from_os_error(exc) from None

    def remove(self, locator: StorageLocator) -> bool:
        """A work directory is removed whole; anything else must be a single file."""
        path = self._path(locator)
        try:
            if path.is_dir() and not path.is_symlink():
                if not locator.key.startswith(f"{WORK_DIR}/"):
                    raise storage_error(OUTSIDE_ROOT)
                shutil.rmtree(path)
                return True
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise from_os_error(exc) from None
        return True

    def open_read(self, locator: StorageLocator) -> BinaryIO:
        try:
            return self._path(locator).open("rb")
        except OSError as exc:
            raise from_os_error(exc) from None

    def playback_target(self, locator: StorageLocator) -> PlaybackTarget:
        return PlaybackTarget("file", str(self._path(locator)))

    def free_space(self) -> int | None:
        try:
            return shutil.disk_usage(self._root).free
        except OSError:
            return None

    def scan(self) -> list[StorageLocator]:
        """Published files and partial directories; reported, never removed, when nothing refers to them."""
        found: list[StorageLocator] = []
        work = self._root / WORK_DIR
        if work.is_dir():
            for job in sorted(work.iterdir()):
                if job.is_dir() and _ID_RE.fullmatch(job.name):
                    found += [
                        StorageLocator(BACKEND, f"{WORK_DIR}/{job.name}/{p.name}/") for p in sorted(job.iterdir()) if p.is_dir()
                    ]
        for dirpath, dirnames, filenames in os.walk(self._root):
            rel = Path(dirpath).relative_to(self._root)
            if rel == Path("."):
                dirnames[:] = [d for d in dirnames if d != WORK_DIR]
            found += [StorageLocator(BACKEND, (rel / f).as_posix()) for f in sorted(filenames)]
        return found


ON_CONFLICT = ("fail", "rename")


class DirectoryWriter:
    """A temporary file in the destination directory, published under a free name only on ``commit``."""

    def __init__(self, dest_dir: Path, file_name: str, *, rename: bool, on_temp: Callable[[Path], None] | None) -> None:
        stem, dot, ext = file_name.rpartition(".")
        self._stem = naming.safe_component(stem if dot else file_name, "export")
        self._ext = naming.safe_extension(ext) if dot else "bin"
        self._dest = dest_dir
        self._rename = rename
        self.temp = dest_dir / f"{EXPORT_PREFIX}{uuid.uuid4().hex}.tmp"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            if on_temp is not None:
                on_temp(self.temp)
            self._out: BinaryIO = self.temp.open("xb")
        except OSError as exc:
            raise from_os_error(exc) from None
        self.path: Path | None = None

    def write(self, chunk: bytes) -> None:
        try:
            self._out.write(chunk)
        except OSError as exc:
            raise from_os_error(exc) from None

    def commit(self) -> str:
        try:
            self._out.flush()
            os.fsync(self._out.fileno())
            self._out.close()
            for attempt in range(1, _MAX_ALLOCATE + 1):
                name = f"{self._stem}.{self._ext}" if attempt == 1 else f"{self._stem} ({attempt}).{self._ext}"
                try:
                    publish_no_replace(self.temp, self._dest / name)
                except StorageError as exc:
                    if exc.code == TARGET_EXISTS and self._rename:
                        continue
                    raise
                self.path = self._dest / name
                return name
            raise storage_error(TARGET_EXISTS)
        except OSError as exc:
            raise from_os_error(exc) from None
        finally:
            self.abort()

    def abort(self) -> None:
        """Safe after ``commit`` too: the temporary name is gone once the file is published."""
        if not self._out.closed:
            self._out.close()
        self.temp.unlink(missing_ok=True)


class DirectoryExportTarget:
    def __init__(self, dest_dir: Path, *, on_temp: Callable[[Path], None] | None = None) -> None:
        self.dest_dir = dest_dir
        self._on_temp = on_temp

    def create(self, name: str, *, on_conflict: str) -> DirectoryWriter:
        if on_conflict not in ON_CONFLICT:
            raise ValueError(f"unknown on_conflict {on_conflict!r}")
        return DirectoryWriter(self.dest_dir, name, rename=on_conflict == "rename", on_temp=self._on_temp)


def copy_to(source: BinaryIO, writer: ExportWriter, cancel: CancelToken = NEVER) -> str:
    """Cancellation counts only before ``commit``: afterwards the file exists and its name is returned."""
    try:
        while chunk := source.read(_CHUNK):
            cancel.raise_if_cancelled()
            writer.write(chunk)
        cancel.raise_if_cancelled()
    except OSError as exc:
        writer.abort()
        raise from_os_error(exc) from None
    except BaseException:
        writer.abort()
        raise
    return writer.commit()


def export_copy(
    source: BinaryIO,
    dest_dir: Path,
    file_name: str,
    *,
    cancel: CancelToken = NEVER,
    rename_on_conflict: bool = True,
    on_temp: Callable[[Path], None] | None = None,
) -> Path:
    """An independent copy (never a link) published without replacing anything; a failed copy leaves no file."""
    target = DirectoryExportTarget(dest_dir, on_temp=on_temp)
    writer = target.create(file_name, on_conflict="rename" if rename_on_conflict else "fail")
    name = copy_to(source, writer, cancel)
    return dest_dir / name
