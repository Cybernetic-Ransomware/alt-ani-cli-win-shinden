"""Debug builds only: the production file store's guarantees checked on the device's own file system and SELinux policy."""

import json
import os
from collections.abc import Callable
from pathlib import Path

from android_storage import MIN_ATOMIC_RENAME_API, AppStorageStore

from alt_ani_core.errors import StorageError
from alt_ani_core.state import StorageLocator
from alt_ani_core.storage.api import NO_ATOMIC_PUBLISH, OUTSIDE_ROOT, TARGET_EXISTS, PublishName

_NAME = PublishName(series_id="1", series_title="Probe", episode_label="1", extension="mp4")


def _code(action: Callable[[], object]) -> str | None:
    try:
        action()
    except StorageError as exc:
        return exc.code
    return None


def _local(base: Path, data: bytes) -> Path:
    path = base / f"local-{len(os.listdir(base))}.bin"
    path.write_bytes(data)
    return path


def run(base: str, api_level: int) -> str:
    """Every check gets its own empty directory under ``base``; the result maps check names to ``{ok, detail}``."""
    root = Path(base)
    results: dict[str, dict] = {}

    def check(name: str, body: Callable[[Path], tuple[bool, str]]) -> None:
        workdir = root / name
        workdir.mkdir(parents=True)
        try:
            ok, detail = body(workdir)
        except Exception as exc:  # a probe reports, it never stops the others
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results[name] = {"ok": ok, "detail": detail}

    def store(workdir: Path, level: int = api_level) -> AppStorageStore:
        return AppStorageStore(workdir / "library", api_level=level)

    def hard_links_refused(workdir: Path) -> tuple[bool, str]:
        (workdir / "a").write_bytes(b"x")
        try:
            os.link(workdir / "a", workdir / "b")
        except OSError as exc:
            return True, f"link refused errno={exc.errno}"
        return False, "link allowed: the core store would have worked here too"

    def root_accepted(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        return (s.root / ".work").is_dir() and os.listdir(s.root / ".work") == [], "renameat2 probe passed"

    def publish_moves(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        local = _local(workdir, b"video")
        target = s.allocate(_NAME)
        s.publish(local, target)
        size = s.stat(target)
        return not local.exists() and size is not None and size.size == 5, f"published {target.key}"

    def existing_target_kept(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        target = s.allocate(_NAME)
        existing = s.root / target.key
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"theirs")
        local = _local(workdir, b"mine")
        code = _code(lambda: s.publish(local, target))
        return code == TARGET_EXISTS and existing.read_bytes() == b"theirs" and local.exists(), f"code={code}"

    def dangling_symlink_kept(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        target = s.allocate(_NAME)
        link = s.root / target.key
        link.parent.mkdir(parents=True)
        link.symlink_to(s.root / "nowhere.mp4")
        code = _code(lambda: s.publish(_local(workdir, b"mine"), target))
        return code == TARGET_EXISTS and link.is_symlink(), f"code={code}"

    def symlink_out_of_root_refused(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        victim = workdir / "victim.mp4"
        victim.write_bytes(b"keep")
        (s.root / "Probe [1]").mkdir()
        (s.root / "Probe [1]" / "Probe - ep1.mp4").symlink_to(victim)
        locator = StorageLocator("fs", "Probe [1]/Probe - ep1.mp4")
        codes = [_code(lambda a=a: a(locator)) for a in (s.stat, s.remove, s.playback_target, s.digest)]
        return set(codes) == {OUTSIDE_ROOT} and victim.read_bytes() == b"keep", f"codes={codes}"

    def removing_a_symlink_keeps_its_target(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        target = s.allocate(_NAME)
        s.publish(_local(workdir, b"video"), target)
        (s.root / "alias.mp4").symlink_to(s.root / target.key)
        removed = s.remove(StorageLocator("fs", "alias.mp4"))
        size = s.stat(target)
        return removed and size is not None and size.size == 5, "alias removed, file kept"

    def free_space_known(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        free = s.free_space()
        return free is not None and free > 0, f"free={free}"

    def work_tidy_up(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        work = s.root / ".work"
        (work / "job-empty").mkdir()
        partial = s.work_path(s.partial_locator("job-full", "p1"))
        (partial / "media.mp4.part").write_bytes(b"x" * 10)
        (work / "notes.txt").write_bytes(b"mine")
        (work / f".probe-{'a' * 32}").write_bytes(b"")
        s.check_root()
        left = sorted(os.listdir(work))
        kept = (partial / "media.mp4.part").read_bytes() == b"x" * 10
        return left == ["job-full", "notes.txt"] and kept, f"left={left}"

    def gate_below_api_30(workdir: Path) -> tuple[bool, str]:
        s = store(workdir, MIN_ATOMIC_RENAME_API - 1)
        code = _code(s.check_root)
        return code == NO_ATOMIC_PUBLISH and not s.root.exists(), f"code={code}, root created={s.root.exists()}"

    def access_denied_is_coded(workdir: Path) -> tuple[bool, str]:
        s = store(workdir)
        s.check_root()
        target = s.allocate(_NAME)
        s.publish(_local(workdir, b"video"), target)
        parent = (s.root / target.key).parent
        os.chmod(parent, 0o500)
        try:
            code = _code(lambda: s.remove(target))
        finally:
            os.chmod(parent, 0o700)
        exists = (s.root / target.key).exists()
        return code == "storage.access_denied" and exists, f"code={code}"

    check("hard_links_refused", hard_links_refused)
    if api_level >= MIN_ATOMIC_RENAME_API:
        check("root_accepted", root_accepted)
        check("publish_moves", publish_moves)
        check("existing_target_kept", existing_target_kept)
        check("dangling_symlink_kept", dangling_symlink_kept)
        check("symlink_out_of_root_refused", symlink_out_of_root_refused)
        check("removing_a_symlink_keeps_its_target", removing_a_symlink_keeps_its_target)
        check("free_space_known", free_space_known)
        check("work_tidy_up", work_tidy_up)
        check("access_denied_is_coded", access_denied_is_coded)
    check("gate_below_api_30", gate_below_api_30)
    return json.dumps({"api_level": api_level, "checks": results})
