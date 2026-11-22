"""The app's file store: app storage refuses hard links (SELinux denies ``link``), so publishing renames instead."""

import ctypes
import errno
import os
import platform
import re
import stat
import sys
import uuid
from json import dumps, loads
from pathlib import Path
from urllib.parse import quote

from alt_ani_bridge import Bridge
from alt_ani_core.state import StorageLocator
from alt_ani_core.storage.api import (
    NO_ATOMIC_PUBLISH,
    TARGET_EXISTS,
    UNAVAILABLE,
    PlaybackTarget,
    from_os_error,
    storage_error,
)
from alt_ani_core.storage.fs import WORK_DIR, FsFileStore, fsync_dir, fsync_file

# renameat2 is in bionic from API 30; below it an unlisted system call may end the process with SIGSYS under seccomp
MIN_ATOMIC_RENAME_API = 30
_RENAMEAT2 = {"x86_64": 316, "aarch64": 276}
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_PROBE_RE = re.compile(r"\.probe-[0-9a-f]{32}(-moved|-link)?")
_JOB_DIR_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def rename_noreplace(src: Path, dst: Path) -> None:
    """``renameat2(RENAME_NOREPLACE)``: atomic and never replaces ``dst``; raises FileExistsError when it exists."""
    number = _RENAMEAT2.get(platform.machine())
    if number is None:
        raise OSError(errno.ENOSYS, "renameat2 number unknown for this machine")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(
        ctypes.c_long(number),
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(src)),
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(dst)),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result != 0:
        code = ctypes.get_errno()
        raise (FileExistsError if code == errno.EEXIST else OSError)(code, os.strerror(code))


class AppStorageStore(FsFileStore):
    """``api_level`` is the device's ``Build.VERSION.SDK_INT``, passed by the host; it is never detected here."""

    def __init__(self, root: Path, authority: str | None = None, name: str | None = None, api_level: int | None = None) -> None:
        super().__init__(root)
        self._prefix = f"content://{authority}/{name}/" if authority and name else None
        self._linux = sys.platform in ("linux", "android")
        self._api_level = api_level

    def _refusal(self) -> str | None:
        if self._api_level is None:
            return "api_level_unknown" if sys.platform == "android" else None
        return "api_level" if self._api_level < MIN_ATOMIC_RENAME_API else None

    def check_root(self) -> None:
        """Only ``OfflineQueue.start`` calls this, holding executor ownership with no attempt running: it also tidies up."""
        refusal = self._refusal()
        if refusal is not None:
            raise storage_error(NO_ATOMIC_PUBLISH, reason=refusal)
        if not self._linux:
            super().check_root()
        else:
            work = self.root / WORK_DIR
            try:
                work.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise storage_error(UNAVAILABLE, errno=exc.errno) from None
            probe = work / f".probe-{uuid.uuid4().hex}"
            moved = probe.with_name(probe.name + "-moved")
            try:
                probe.write_bytes(b"")
                rename_noreplace(probe, moved)
            except OSError as exc:
                raise storage_error(NO_ATOMIC_PUBLISH, errno=exc.errno) from None
            finally:
                for path in (moved, probe):
                    path.unlink(missing_ok=True)
        self._tidy_work()

    def _tidy_work(self) -> None:
        """Probe files a crash left behind and empty job directories; never recursive, never a symlink, never anything else."""
        work = self.root / WORK_DIR
        try:
            names = os.listdir(work)
        except OSError:
            return
        for name in names:
            path = work / name
            try:
                mode = os.lstat(path).st_mode
                if _PROBE_RE.fullmatch(name) and stat.S_ISREG(mode):
                    path.unlink()
                elif _JOB_DIR_RE.fullmatch(name) and stat.S_ISDIR(mode):
                    # rmdir only removes an empty directory, so a job's partial is never touched
                    os.rmdir(path)
            except OSError:
                continue

    def publish(self, local: Path, target: StorageLocator) -> None:
        refusal = self._refusal()
        if refusal is not None:
            raise storage_error(NO_ATOMIC_PUBLISH, reason=refusal)
        if not self._linux:
            super().publish(local, target)
            return
        dst = Path(super().playback_target(target).value)
        try:
            fsync_file(local)
            dst.parent.mkdir(parents=True, exist_ok=True)
            rename_noreplace(local, dst)
            fsync_dir(dst.parent)
        except FileExistsError:
            raise storage_error(TARGET_EXISTS) from None
        except OSError as exc:
            raise from_os_error(exc) from None

    def playback_target(self, locator: StorageLocator) -> PlaybackTarget:
        target = super().playback_target(locator)
        if self._prefix is None:
            return target
        relative = Path(target.value).relative_to(self.root).as_posix()
        return PlaybackTarget("content_uri", self._prefix + quote(relative, safe="/"))


def register(
    bridge: Bridge, root: str, authority: str | None = None, name: str | None = None, api_level: int | None = None
) -> str:
    """The ``storage`` argument for ``create_engine``; the handle lives as long as ``bridge``."""
    store = AppStorageStore(Path(root), authority, name, api_level)
    reply = loads(bridge.register_platform_object(store))
    return dumps({"kind": "platform", "handle": reply["data"]["handle"]})
