"""The executor end to end on a local server and controllable fakes; no hosting, no Android."""

import threading

import pytest

from alt_ani_core.errors import ExecutorBusyError
from alt_ani_core.models import Stream, StreamProtocol, StreamVariant
from alt_ani_core.services.offline.events import InteractionRequired
from alt_ani_core.services.offline.interaction import InteractionGrant, InteractionNeeded
from alt_ani_core.services.offline.sources import SourceUnavailable
from alt_ani_core.state import (
    AlternativesPolicy,
    DownloadCriteria,
    ItemStatus,
    JobState,
    NetworkKind,
    OpenedVia,
    QualityCriterion,
    SelectionMode,
    WaitReason,
)
from alt_ani_core.testing import RecordingSink
from alt_ani_core.transfer import TransferFailed

from .servers import SECRET, Resource, ServerState, random_body, serve
from .support import SERIES, FakeSources, GatedTransfer, episode, make_queue, player, progressive, wait_for

pytestmark = pytest.mark.loopback


def _job_state(state, job_id):
    return state.queue.get(job_id).state


def _single(url: str) -> FakeSources:
    return FakeSources([player("1")], {"1": lambda _fresh: progressive(f"{url}/v.mp4?t={SECRET}")})


@pytest.mark.unit
class TestHappyPath:
    def test_job_goes_from_queue_to_an_available_offline_copy(self, tmp_path):
        body = random_body(400_000)
        server = ServerState(resources={"/v.mp4": Resource(body, '"e1"')})
        with serve(server) as url:
            queue, state, _ = make_queue(tmp_path, _single(url))
            state.watch.record_opened(SERIES, episode(1), OpenedVia.PLAYER)
            (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
            queue.start()
            try:
                wait_for(lambda: _job_state(state, job.id) is JobState.COMPLETED)
            finally:
                assert queue.close()
        item = state.library.get(state.queue.get(job.id).offline_item_id)
        assert item.status is ItemStatus.AVAILABLE and item.spec.sha256 and item.spec.size_bytes == len(body)
        assert (tmp_path / "library" / item.spec.locator.key).read_bytes() == body
        assert item.spec.locator.key == "Fate Zero [7]/Fate Zero - ep1.mp4"
        assert state.watch.get(job.key).opened_via is OpenedVia.PLAYER
        wait_for(lambda: not state.queue.pending_removals() or queue.start() is None)

    def test_second_executor_for_the_same_database_is_refused(self, tmp_path):
        queue, state, _ = make_queue(tmp_path, FakeSources([], {}))
        queue.start()
        try:
            other, _, _ = make_queue(tmp_path, FakeSources([], {}))
            with pytest.raises(ExecutorBusyError) as exc:
                other.start()
            assert exc.value.code == "executor.busy"
        finally:
            queue.close()


@pytest.mark.unit
class TestStopping:
    def _running(self, tmp_path, gate: GatedTransfer, **kwargs):
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, network = make_queue(tmp_path, sources, transfer=gate, **kwargs)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        assert gate.started.acquire(timeout=10)
        return queue, state, network, job

    def test_no_second_attempt_before_the_first_ends_its_io(self, tmp_path):
        gate = GatedTransfer()
        queue, state, _, job = self._running(tmp_path, gate)
        try:
            queue.pause(job.id)
            assert _job_state(state, job.id) is JobState.STOPPING
            threading.Event().wait(0.3)
            assert gate.calls == 1 and _job_state(state, job.id) is JobState.STOPPING
            gate.io_done.set()
            wait_for(lambda: _job_state(state, job.id) is JobState.PAUSED)
            assert gate.max_active == 1
        finally:
            gate.release.set()
            gate.io_done.set()
            queue.close()

    @pytest.mark.parametrize("action", ["cancel", "pause"])
    def test_user_stop_wins_and_settles_after_io(self, tmp_path, action):
        gate = GatedTransfer()
        queue, state, _, job = self._running(tmp_path, gate)
        try:
            getattr(queue, action)(job.id)
            gate.io_done.set()
            expected = JobState.CANCELLED if action == "cancel" else JobState.PAUSED
            wait_for(lambda: _job_state(state, job.id) is expected)
        finally:
            gate.release.set()
            queue.close()
        if action == "cancel":
            assert state.queue.pending_removals() == [] or all(
                r.reason.value == "cancelled" for r in state.queue.pending_removals()
            )

    def test_pause_while_resolving_the_source_ends_without_a_transfer(self, tmp_path):
        resolving = threading.Event()

        def slow_stream(_fresh):
            resolving.set()
            threading.Event().wait(0.5)
            return progressive("http://127.0.0.1:9/v.mp4")

        class CancellableEpisode:
            entries = [player("1")]

            def resolve(self, entry, *, fresh, cancel):
                resolving.set()
                for _ in range(500):
                    cancel.raise_if_cancelled()
                    threading.Event().wait(0.01)
                return slow_stream(fresh)

        sources = FakeSources([player("1")], {})
        sources.episode = CancellableEpisode()
        gate = GatedTransfer()
        queue, state, _ = make_queue(tmp_path, sources, transfer=gate)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            assert resolving.wait(10)
            queue.pause(job.id)
            wait_for(lambda: _job_state(state, job.id) is JobState.PAUSED, timeout=5)
            assert gate.calls == 0
        finally:
            queue.close()

    def test_stop_from_another_process_is_seen_by_the_monitor(self, tmp_path):
        gate = GatedTransfer()
        queue, state, _, job = self._running(tmp_path, gate)
        try:
            from alt_ani_core.history.local import open_local_state

            open_local_state(tmp_path / "history.db").queue.pause(job.id)
            gate.io_done.set()
            wait_for(lambda: _job_state(state, job.id) is JobState.PAUSED, timeout=5)
        finally:
            gate.release.set()
            queue.close()

    def test_close_keeps_pending_stops_and_requeues_plain_running_jobs(self, tmp_path):
        gate = GatedTransfer()
        gate.io_done.set()
        queue, state, network, job = self._running(tmp_path, gate)
        queue.close()
        assert _job_state(state, job.id) is JobState.QUEUED
        assert state.queue.get(job.id).retry_count == 0

    def test_close_keeps_a_network_wait(self, tmp_path):
        gate = GatedTransfer()
        queue, state, network, job = self._running(tmp_path, gate)
        state.queue.hold(job.id, WaitReason.NETWORK)
        gate.io_done.set()
        queue.close()
        settled = state.queue.get(job.id)
        assert (settled.state, settled.wait_reason) == (JobState.WAITING, WaitReason.NETWORK)


@pytest.mark.unit
class TestNetwork:
    def test_wifi_only_by_default_and_mobile_data_stops_the_transfer(self, tmp_path):
        gate = GatedTransfer()
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, network = make_queue(tmp_path, sources, transfer=gate)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            assert gate.started.acquire(timeout=10)
            network.set(NetworkKind.CELLULAR)
            wait_for(lambda: _job_state(state, job.id) is JobState.STOPPING)
            gate.io_done.set()
            wait_for(lambda: state.queue.get(job.id).wait_reason is WaitReason.NETWORK)
            assert sources.networks[-1] is NetworkKind.CELLULAR
            gate.io_done.clear()
            network.set(NetworkKind.WIFI)
            assert gate.started.acquire(timeout=10)
            gate.release.set()
            wait_for(lambda: _job_state(state, job.id) is JobState.COMPLETED)
            assert gate.resumed == [False, True]
        finally:
            gate.release.set()
            gate.io_done.set()
            queue.close()

    def test_consent_belongs_to_one_job_and_revoking_it_stops_the_transfer(self, tmp_path):
        gate = GatedTransfer()
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, network = make_queue(tmp_path, sources, transfer=gate)
        network.set(NetworkKind.CELLULAR)
        first, second = queue.enqueue(SERIES, [episode(1), episode(2)], DownloadCriteria()).added
        queue.allow_cellular(second.id, True)
        queue.start()
        try:
            assert gate.started.acquire(timeout=10)
            running = state.queue.jobs([JobState.RUNNING])
            assert [j.id for j in running] == [second.id]
            assert state.queue.get(first.id).wait_reason is WaitReason.NETWORK
            queue.allow_cellular(second.id, False)
            wait_for(lambda: _job_state(state, second.id) is JobState.STOPPING)
            gate.io_done.set()
            wait_for(lambda: state.queue.get(second.id).wait_reason is WaitReason.NETWORK)
        finally:
            gate.release.set()
            gate.io_done.set()
            queue.close()

    def test_manual_pause_survives_network_flapping(self, tmp_path):
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, network = make_queue(tmp_path, sources, transfer=GatedTransfer())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.pause(job.id)
        queue.start()
        try:
            for kind in (NetworkKind.OFFLINE, NetworkKind.CELLULAR, NetworkKind.WIFI, NetworkKind.OFFLINE, NetworkKind.WIFI):
                network.set(kind)
                threading.Event().wait(0.05)
            threading.Event().wait(0.2)
            assert _job_state(state, job.id) is JobState.PAUSED
        finally:
            queue.close()


@pytest.mark.unit
class TestConditions:
    def test_interaction_then_offline_then_wifi(self, tmp_path):
        sink = RecordingSink()
        sources = FakeSources([player("1")], {})
        sources.open_error = InteractionNeeded("shinden")
        queue, state, network = make_queue(tmp_path, sources, events=sink, transfer=GatedTransfer())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).wait_reason is WaitReason.INTERACTION)
            assert sink.of(InteractionRequired)[0].scope == "shinden"
            network.set(NetworkKind.OFFLINE)
            threading.Event().wait(0.1)
            assert state.queue.get(job.id).wait_reason is WaitReason.INTERACTION
            sources.open_error = None
            queue.complete_interaction(job.id, InteractionGrant("shinden", values={"cf": SECRET}))
            wait_for(lambda: state.queue.get(job.id).wait_reason is WaitReason.NETWORK)
        finally:
            queue.close()
        assert SECRET not in repr(state.queue.get(job.id))

    def test_full_disk_waits_and_holds_others_until_space_is_reported(self, tmp_path):
        gate = GatedTransfer()
        gate.fail_with = TransferFailed("storage.no_space")
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, network = make_queue(tmp_path, sources, transfer=gate)
        first, second = queue.enqueue(SERIES, [episode(1), episode(2)], DownloadCriteria()).added
        queue.start()
        try:
            gate.release.set()
            wait_for(lambda: state.queue.get(first.id).wait_reason is WaitReason.STORAGE)
            wait_for(lambda: state.queue.get(second.id).wait_reason is WaitReason.STORAGE)
            calls = gate.calls
            threading.Event().wait(0.3)
            assert gate.calls == calls
            gate.fail_with = None
            queue.notify_storage_changed()
            wait_for(lambda: _job_state(state, first.id) is JobState.COMPLETED)
            wait_for(lambda: _job_state(state, second.id) is JobState.COMPLETED)
        finally:
            queue.close()

    def test_transient_failure_is_deferred_and_bounded(self, tmp_path):
        gate = GatedTransfer()
        gate.fail_with = TransferFailed("timeout")
        gate.release.set()
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, _ = make_queue(tmp_path, sources, transfer=gate)
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).retry_count == 1)
            deferred = state.queue.get(job.id)
            assert deferred.state is JobState.QUEUED and deferred.not_before is not None
            threading.Event().wait(0.3)
            assert gate.calls == 1
        finally:
            queue.close()


@pytest.mark.unit
class TestSourceSelection:
    def test_only_sources_matching_the_job_criteria_are_used(self, tmp_path):
        server = ServerState(resources={"/v.mp4": Resource(random_body(50_000), '"e"')})
        with serve(server) as url:
            sources = FakeSources(
                [player("pl", audio="pl"), player("jp", audio="jp")],
                {"pl": lambda _f: progressive(f"{url}/bad"), "jp": lambda _f: progressive(f"{url}/v.mp4")},
            )
            queue, state, _ = make_queue(tmp_path, sources)
            (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria(audio="jp")).added
            queue.start()
            try:
                wait_for(lambda: _job_state(state, job.id) is JobState.COMPLETED)
            finally:
                queue.close()
        assert [r[0] for r in sources.episode.resolved] == ["jp"]
        assert state.library.items()[0].spec.audio == "jp"

    def test_no_variant_within_the_limit_waits_for_a_decision(self, tmp_path):
        stream = Stream(
            variants=(
                StreamVariant("http://127.0.0.1:9/a", StreamProtocol.PROGRESSIVE, label="1080p"),
                StreamVariant("http://127.0.0.1:9/b", StreamProtocol.PROGRESSIVE, label=""),
            )
        )
        sources = FakeSources([player("1")], {"1": lambda _f: stream})
        queue, state, _ = make_queue(tmp_path, sources, transfer=GatedTransfer())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria(quality=QualityCriterion(720))).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).wait_reason is WaitReason.SOURCE_DECISION)
        finally:
            queue.close()
        assert state.queue.get(job.id).last_error.code == "criteria.no_match"

    def test_unavailable_sources_defer_instead_of_failing_at_once(self, tmp_path):
        sources = FakeSources([player("1")], {"1": lambda _f: (_ for _ in ()).throw(SourceUnavailable("exhausted"))})
        queue, state, _ = make_queue(tmp_path, sources, transfer=GatedTransfer())
        (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).retry_count == 1)
        finally:
            queue.close()


@pytest.mark.unit
class TestResumeAcrossRestarts:
    def _stop_midway(self, tmp_path, url, body_size=3_000_000):
        server_body = random_body(body_size)
        return server_body

    @pytest.mark.parametrize(
        ("policy", "changed", "expected"),
        [
            (AlternativesPolicy.OFF, False, JobState.COMPLETED),
            (AlternativesPolicy.OFF, True, JobState.WAITING),
            (AlternativesPolicy.COMPATIBLE, True, JobState.COMPLETED),
        ],
    )
    def test_restart_resumes_with_evidence_or_follows_the_policy(self, tmp_path, policy, changed, expected):
        body = random_body(3_000_000)
        server = ServerState(resources={"/v.mp4": Resource(body, '"e1"')}, stall_after=1_000_000)
        with serve(server) as url:
            queue, state, _ = make_queue(tmp_path, _single(url))
            (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria(), alternatives=policy).added
            queue.start()
            wait_for(
                lambda: (p := state.queue.get(job.id).partial) and p.validator and dict(p.validator.fields).get("length", 0) > 0
            )
            assert queue.close(timeout=10)
            assert _job_state(state, job.id) is JobState.QUEUED
            server.stall_after = None
            server.stall.set()
            if changed:
                server.resources["/v.mp4"] = Resource(body[::-1], '"e2"')
            again, state, _ = make_queue(tmp_path, _single(url))
            again.start()
            try:
                wait_for(lambda: _job_state(state, job.id) is expected)
            finally:
                again.close()
        settled = state.queue.get(job.id)
        if expected is JobState.WAITING:
            assert settled.wait_reason is WaitReason.SOURCE_DECISION
            assert settled.partial is not None
        else:
            item = state.library.get(settled.offline_item_id)
            data = (tmp_path / "library" / item.spec.locator.key).read_bytes()
            assert data == (body[::-1] if changed else body)
            if not changed:
                assert any(entry[3] == '"e1"' for entry in server.log if entry[2])

    def test_manual_selection_never_restarts_unasked(self, tmp_path):
        sources = FakeSources([player("1")], {"1": lambda _f: progressive("http://127.0.0.1:9/v.mp4")})
        queue, state, _ = make_queue(tmp_path, sources, transfer=GatedTransfer())
        from alt_ani_core.rules.criteria import source_identity

        from .support import player as make_player

        (job,) = queue.enqueue(
            SERIES,
            [episode(1)],
            DownloadCriteria(),
            alternatives=AlternativesPolicy.COMPATIBLE,
            selection_mode=SelectionMode.MANUAL,
            source=source_identity(make_player("gone", name="vanished-host")),
        ).added
        queue.start()
        try:
            wait_for(lambda: state.queue.get(job.id).wait_reason is WaitReason.SOURCE_DECISION)
        finally:
            queue.close()
        assert state.queue.get(job.id).last_error.code == "source.gone"


@pytest.mark.unit
class TestNoSecretsPersisted:
    def test_urls_tokens_and_etags_never_reach_the_database_or_events(self, tmp_path):
        sink = RecordingSink()
        server = ServerState(resources={"/v.mp4": Resource(random_body(200_000), f'"{SECRET}"')})
        with serve(server) as url:
            queue, state, _ = make_queue(tmp_path, _single(url), events=sink)
            (job,) = queue.enqueue(SERIES, [episode(1)], DownloadCriteria()).added
            queue.start()
            try:
                wait_for(lambda: _job_state(state, job.id) is JobState.COMPLETED)
            finally:
                queue.close()
        for path in tmp_path.glob("history.db*"):
            assert SECRET.encode() not in path.read_bytes()
        assert SECRET not in repr(sink.events)
