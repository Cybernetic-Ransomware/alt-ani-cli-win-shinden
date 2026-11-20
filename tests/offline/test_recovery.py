"""Start-up recovery from the states a crash can leave at each step of publishing, deleting and recovering."""

import hashlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from alt_ani_core.history.local import open_local_state
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.rules.resume import complete_evidence
from alt_ani_core.services.offline.events import OrphanFound
from alt_ani_core.state import (
    DownloadCriteria,
    ItemStatus,
    JobState,
    NetworkKind,
    PartialTransfer,
    SourceIdentity,
    StorageLocator,
    VariantIdentity,
)
from alt_ani_core.storage.fs import FsFileStore
from alt_ani_core.testing import RecordingSink

from .servers import Resource, ServerState, random_body, serve
from .support import SERIES, FakeSources, episode, make_queue, player, progressive, wait_for

SOURCE = SourceIdentity("1", "host", "jp", "pl", "1080p", "", "")
VARIANT = VariantIdentity("720p", 720, "progressive", "mp4")
BODY = b"published-video" * 1000
TARGET = StorageLocator("fs", "Fate Zero [7]/Fate Zero - ep1.mp4")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Crashed:
    """Drives the store and files to the state a crash at a given point leaves, then lets the process 'die'."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.state = open_local_state(tmp_path / "history.db")
        self.store = FsFileStore(tmp_path / "library")
        self.store.check_root()

    def finalizing(self, *, evidence: bool = True):
        (job,) = self.state.queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        owner = ExecutorOwnership.try_acquire(self.tmp / "history.db")
        claim = self.state.queue.claim_next(NetworkKind.WIFI, owner)
        locator = self.store.partial_locator(job.id, "p1")
        media = self.store.work_path(locator) / "media.mp4"
        media.write_bytes(BODY)
        validator = complete_evidence(len(BODY), _sha(BODY)) if evidence else None
        self.state.queue.set_partial(claim.attempt_id, PartialTransfer(locator, SOURCE, VARIANT, validator), len(BODY))
        self.state.queue.set_target(claim.attempt_id, TARGET)
        assert self.state.queue.begin_finalize(claim.attempt_id)
        owner.release()
        return job, media

    def target_path(self) -> Path:
        return self.tmp / "library" / TARGET.key

    def recover(self, sink: RecordingSink | None = None):
        queue, state, _ = make_queue(self.tmp, FakeSources([], {}), events=sink or RecordingSink())
        report = queue.start()
        queue.close()
        return report, state


@pytest.mark.unit
class TestFinalizingAfterACrash:
    def test_published_but_not_completed_is_completed_from_its_evidence(self, tmp_path):
        crash = Crashed(tmp_path)
        job, media = crash.finalizing()
        target = crash.target_path()
        target.parent.mkdir(parents=True)
        os.link(media, target) if os.name == "posix" else target.write_bytes(BODY)
        report, state = crash.recover()
        assert report.completed == (job.id,)
        item = state.library.get(state.queue.get(job.id).offline_item_id)
        assert (item.status, item.spec.sha256) == (ItemStatus.AVAILABLE, _sha(BODY))
        assert not media.parent.exists()

    def test_not_yet_published_is_published_then_completed(self, tmp_path):
        crash = Crashed(tmp_path)
        job, _ = crash.finalizing()
        report, state = crash.recover()
        assert report.completed == (job.id,)
        assert crash.target_path().read_bytes() == BODY

    def test_foreign_file_at_the_target_is_left_and_the_job_fails(self, tmp_path):
        crash = Crashed(tmp_path)
        job, _ = crash.finalizing()
        target = crash.target_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * len(BODY))
        report, state = crash.recover()
        assert report.failed == (job.id,)
        assert state.queue.get(job.id).last_error.code == "storage.location_conflict"
        assert target.read_bytes() == b"x" * len(BODY)

    def test_without_evidence_nothing_is_completed(self, tmp_path):
        crash = Crashed(tmp_path)
        job, _ = crash.finalizing(evidence=False)
        report, state = crash.recover()
        assert state.queue.get(job.id).last_error.code == "recovery.evidence_missing"
        assert not crash.target_path().exists()

    def test_corrupted_work_file_is_not_published(self, tmp_path):
        crash = Crashed(tmp_path)
        job, media = crash.finalizing()
        media.write_bytes(b"y" * len(BODY))
        report, state = crash.recover()
        assert report.failed == (job.id,)
        assert not crash.target_path().exists()

    def test_recovering_twice_ends_the_same_way(self, tmp_path):
        crash = Crashed(tmp_path)
        job, _ = crash.finalizing()
        crash.recover()
        report, state = crash.recover()
        assert state.queue.get(job.id).state is JobState.COMPLETED
        assert len(state.library.items()) == 1 and report.completed == ()


@pytest.mark.unit
class TestInterruptedAttempts:
    def test_running_is_requeued_and_stopping_settles(self, tmp_path):
        crash = Crashed(tmp_path)
        queue = crash.state.queue
        first, second = queue.enqueue(SERIES, [episode(1), episode(2)], DownloadCriteria()).added
        crash.state.preferences.update(max_active_downloads=2)
        owner = ExecutorOwnership.try_acquire(tmp_path / "history.db")
        queue.claim_next(NetworkKind.WIFI, owner)
        queue.claim_next(NetworkKind.WIFI, owner)
        queue.pause(second.id)
        owner.release()
        report, state = crash.recover()
        assert report.requeued == (first.id,) and report.settled == (second.id,)
        assert state.queue.get(second.id).state is JobState.PAUSED


@pytest.mark.unit
class TestLibraryReconciliation:
    def _item(self, crash: Crashed):
        job, _ = crash.finalizing()
        crash.recover()
        return crash.state.library.get(crash.state.queue.get(job.id).offline_item_id)

    def test_vanished_file_marks_the_item_missing(self, tmp_path):
        crash = Crashed(tmp_path)
        item = self._item(crash)
        crash.target_path().unlink()
        report, state = crash.recover()
        assert report.marked_missing == (item.id,)
        assert state.library.get(item.id).status is ItemStatus.MISSING

    def test_same_copy_returning_is_restored_but_same_size_other_content_is_not(self, tmp_path):
        crash = Crashed(tmp_path)
        item = self._item(crash)
        crash.target_path().unlink()
        crash.recover()
        crash.target_path().write_bytes(b"z" * len(BODY))
        _, state = crash.recover()
        assert state.library.get(item.id).status is ItemStatus.MISSING
        crash.target_path().write_bytes(BODY)
        report, state = crash.recover()
        assert report.restored == (item.id,)
        assert state.library.get(item.id).status is ItemStatus.AVAILABLE

    def test_interrupted_delete_is_finished(self, tmp_path):
        crash = Crashed(tmp_path)
        item = self._item(crash)
        crash.state.library.mark_deleting([item.id])
        report, state = crash.recover()
        assert report.deletes_finished == (item.id,)
        assert state.library.items() == [] and not crash.target_path().exists()

    def test_delete_whose_file_was_already_removed_is_finished(self, tmp_path):
        crash = Crashed(tmp_path)
        item = self._item(crash)
        crash.state.library.mark_deleting([item.id])
        crash.target_path().unlink()
        _, state = crash.recover()
        assert state.library.items() == []

    def test_delete_keeps_a_different_file_found_at_the_locator(self, tmp_path):
        crash = Crashed(tmp_path)
        item = self._item(crash)
        crash.state.library.mark_deleting([item.id])
        crash.target_path().write_bytes(b"q" * len(BODY))
        sink = RecordingSink()
        _, state = crash.recover(sink)
        assert state.library.items() == []
        assert crash.target_path().exists()
        assert TARGET.key in [e.key for e in sink.of(OrphanFound)]

    def test_unknown_files_are_reported_and_kept(self, tmp_path):
        crash = Crashed(tmp_path)
        stray = tmp_path / "library" / "mine.mkv"
        stray.write_bytes(b"user file")
        unknown_work = tmp_path / "library" / ".work" / "deadbeef" / "p9"
        unknown_work.mkdir(parents=True)
        sink = RecordingSink()
        report, _ = crash.recover(sink)
        assert {loc.key for loc in report.orphans} == {"mine.mkv", ".work/deadbeef/p9/"}
        assert stray.exists() and unknown_work.exists()
        assert len(sink.of(OrphanFound)) == 2


@pytest.mark.unit
@pytest.mark.loopback
class TestRealCrash:
    def test_process_killed_after_publishing_before_completing(self, tmp_path):
        body = random_body(300_000)
        server = ServerState(resources={"/v.mp4": Resource(body, '"e1"')})
        with serve(server) as url:
            script = textwrap.dedent(
                f"""
                import os, sys
                from pathlib import Path
                sys.path.insert(0, {str(Path(__file__).parents[2])!r})
                from alt_ani_core.storage import fs
                from alt_ani_core.models import StreamProtocol
                from alt_ani_core.state import DownloadCriteria
                from tests.offline.support import SERIES, FakeSources, episode, make_queue, player, progressive

                real = fs.FsFileStore.publish
                def publish_then_die(self, local, target):
                    real(self, local, target)
                    os._exit(7)
                fs.FsFileStore.publish = publish_then_die
                sources = FakeSources([player("1")], {{"1": lambda _f: progressive({url!r} + "/v.mp4")}})
                queue, state, _ = make_queue(Path(sys.argv[1]), sources)
                queue.enqueue(SERIES, [episode(1)], DownloadCriteria())
                queue.start()
                import time
                time.sleep(60)
                """
            )
            result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=90, check=False)
        assert result.returncode == 7
        crash = Crashed(tmp_path)
        (job,) = crash.state.queue.jobs()
        assert job.state is JobState.FINALIZING
        report, state = crash.recover()
        assert report.completed == (job.id,)
        item = state.library.get(state.queue.get(job.id).offline_item_id)
        assert (tmp_path / "library" / item.spec.locator.key).read_bytes() == body


@pytest.mark.unit
@pytest.mark.loopback
class TestKilledMidTransfer:
    def test_process_killed_after_a_checkpoint_resumes_byte_identical(self, tmp_path):
        body = random_body(4_000_000)
        server = ServerState(resources={"/v.mp4": Resource(body, '"e1"')})
        with serve(server) as url:
            script = textwrap.dedent(
                f"""
                import os, sys
                from pathlib import Path
                sys.path.insert(0, {str(Path(__file__).parents[2])!r})
                from alt_ani_core.history.queue import SqliteDownloadQueue
                from alt_ani_core.state import DownloadCriteria
                from tests.offline.support import SERIES, FakeSources, episode, make_queue, player, progressive

                real = SqliteDownloadQueue.set_partial
                def set_partial_then_die(self, attempt_id, partial, bytes_done=None):
                    status = real(self, attempt_id, partial, bytes_done)
                    if bytes_done and bytes_done > 1_000_000:
                        os._exit(9)
                    return status
                SqliteDownloadQueue.set_partial = set_partial_then_die
                sources = FakeSources([player("1")], {{"1": lambda _f: progressive({url!r} + "/v.mp4")}})
                queue, state, _ = make_queue(Path(sys.argv[1]), sources)
                queue.enqueue(SERIES, [episode(1)], DownloadCriteria())
                queue.start()
                import time
                time.sleep(60)
                """
            )
            result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=90, check=False)
            assert result.returncode == 9
            sources = FakeSources([player("1")], {"1": lambda _f: progressive(f"{url}/v.mp4")})
            queue, state, _ = make_queue(tmp_path, sources)
            report = queue.start()
            try:
                assert len(report.requeued) == 1
                job_id = report.requeued[0]
                wait_for(lambda: state.queue.get(job_id).state is JobState.COMPLETED, timeout=30)
            finally:
                queue.close()
        item = state.library.get(state.queue.get(job_id).offline_item_id)
        assert (tmp_path / "library" / item.spec.locator.key).read_bytes() == body
        assert any(entry[2] and entry[3] == '"e1"' for entry in server.log)
