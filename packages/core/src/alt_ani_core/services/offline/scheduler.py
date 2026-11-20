"""Scheduler loop: monitors attempts, applies conditions, cleans up and claims; sleeps on a condition variable."""

import threading
from datetime import datetime

from alt_ani_core.history.database import STORE_ERRORS
from alt_ani_core.history.ownership import ExecutorOwnership
from alt_ani_core.rules.conditions import CONDITION_REASONS, Conditions, blocking_condition, storage_ok
from alt_ani_core.rules.network import download_allowed
from alt_ani_core.services.offline.attempt import Attempt, Context
from alt_ani_core.services.offline.cleanup import remove_pending
from alt_ani_core.services.offline.events import AttemptStuck
from alt_ani_core.state import ACTIVE_STATES, DownloadJob, DownloadNetwork, JobState, NetworkKind, WaitReason


class Scheduler:
    def __init__(self, ctx: Context, owner: ExecutorOwnership) -> None:
        self._ctx = ctx
        self._owner = owner
        self._cond = threading.Condition()
        self._woken = False
        self._closing = False
        self._attempts: dict[str, Attempt] = {}
        self._storage_notified = False
        self._unreadable_since: float | None = None
        self._network = ctx.network.current()
        self._thread = threading.Thread(target=self._loop, name="offline-scheduler", daemon=True)
        self.ticks = 0

    @property
    def active(self) -> list[Attempt]:
        with self._cond:
            return list(self._attempts.values())

    def start(self) -> None:
        self._ctx.sources.network_changed(self._network)
        self._ctx.network.subscribe(self._on_network)
        self._thread.start()

    def wake(self) -> None:
        with self._cond:
            self._woken = True
            self._cond.notify_all()

    def notify_storage_changed(self) -> None:
        with self._cond:
            self._storage_notified = True
        self.wake()

    def _on_network(self, network: NetworkKind) -> None:
        self._ctx.sources.network_changed(network)
        self.wake()

    def close(self, timeout: float | None = None) -> bool:
        """Stops claiming and every attempt; False while the loop or some attempt's I/O still runs, so it may be retried."""
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout)
        # a loop still inside a pass may claim after the snapshot below; only its end makes the snapshot final
        ended = not self._thread.is_alive()
        attempts = self.active
        for attempt in attempts:
            attempt.stop("shutdown")
        wait = self._ctx.config.stuck_after_sec if timeout is None else timeout
        for attempt in attempts:
            if attempt.thread.ident is None:
                ended = False
                continue
            attempt.thread.join(wait)
            if attempt.thread.is_alive():
                ended = False
                self._ctx.events.emit(AttemptStuck(job_id=attempt.job.id))
        if ended:
            # a last try for outcomes the store refused; whatever stays unwritten is recovered at the next start
            self._reap()
        return ended

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._closing:
                    return
            try:
                timeout = self.tick()
            except STORE_ERRORS:
                timeout = self._ctx.config.monitor_interval_sec
            with self._cond:
                if self._closing:
                    return
                if not self._woken:
                    self._cond.wait(timeout)
                self._woken = False

    def tick(self) -> float:
        """One pass; returns how long the loop may sleep before the next one."""
        self.ticks += 1
        ctx = self._ctx
        self._reap()
        network = ctx.network.current()
        if network is not self._network:
            self._network = network
        try:
            prefs = ctx.state.preferences.get()
            rows = {job.attempt_id: job for job in ctx.state.queue.jobs(ACTIVE_STATES)}
        except STORE_ERRORS:
            self._unreadable(True)
            raise
        self._unreadable(False)
        self._monitor(network, prefs.download_network, rows)
        with self._cond:
            notified, self._storage_notified = self._storage_notified, False
        claimable = self._apply_conditions(Conditions(network, prefs.download_network, ctx.store.free_space(), notified))
        in_use = {a.partial.locator for a in self.active if a.partial is not None}
        remove_pending(ctx.state.queue, ctx.store, in_use)
        if claimable:
            self._claim(network)
        return self._sleep_for()

    def _reap(self) -> None:
        """An attempt whose outcome the store refused stays here, keeping its slot, until a retry writes it."""
        for attempt in self.active:
            if attempt.io_ended and not attempt.done:
                attempt.settle_pending()
        with self._cond:
            for attempt_id in [k for k, a in self._attempts.items() if a.done]:
                del self._attempts[attempt_id]

    def _monitor(self, network: NetworkKind, policy: DownloadNetwork, rows: dict[str | None, DownloadJob]) -> None:
        """Frequent and read-only: a stop recorded by anyone, or a network the job may not use, ends the I/O."""
        queue = self._ctx.state.queue
        for attempt in self.active:
            job = rows.get(attempt.attempt_id)
            if job is None:
                attempt.lost = True
                attempt.stop("lost")
            elif job.state is JobState.STOPPING:
                attempt.stop()
            elif job.state is JobState.RUNNING and not download_allowed(network, policy, job.cellular_allowed):
                queue.hold(job.id, WaitReason.NETWORK)
                attempt.stop()

    def _unreadable(self, failed: bool) -> None:
        """Without reads the executor cannot confirm the transfer is still allowed, so it stops after a while."""
        if not failed:
            self._unreadable_since = None
            return
        now = self._ctx.clock()
        if self._unreadable_since is None:
            self._unreadable_since = now
        elif now - self._unreadable_since >= self._ctx.config.unreadable_stop_sec:
            for attempt in self.active:
                attempt.stop("store_unreadable")

    def _apply_conditions(self, conditions: Conditions) -> bool:
        """Returns whether some job may be claimed now; an idle pass then opens no write transaction at all."""
        queue = self._ctx.state.queue
        jobs = queue.jobs([JobState.QUEUED, JobState.WAITING])
        storage_blocked = any(j.state is JobState.WAITING and j.wait_reason is WaitReason.STORAGE for j in jobs)
        now = self._ctx.wall().isoformat()
        claimable = False
        for job in jobs:
            queued = self._apply(job, conditions, storage_blocked)
            claimable |= queued and (job.not_before is None or job.not_before <= now)
        return claimable

    def _apply(self, job: DownloadJob, conditions: Conditions, storage_blocked: bool) -> bool:
        """True when the job ends up queued."""
        queue = self._ctx.state.queue
        blocking = blocking_condition(job, conditions, storage_blocked=storage_blocked)
        if job.state is JobState.WAITING:
            reason = job.wait_reason
            if reason is None or reason not in CONDITION_REASONS or reason is blocking:
                return False
            if reason is WaitReason.STORAGE and not storage_ok(job, conditions):
                # only evidence lifts a full disk; the stored reason must survive a restart
                return False
            # one stored reason: lifting it re-checks the others before the job can be claimed
            if not queue.release(job.id, reason):
                return False
            if blocking is not None:
                queue.hold(job.id, blocking)
                return False
            return True
        if blocking is not None:
            queue.hold(job.id, blocking)
            return False
        return True

    def _claim(self, network: NetworkKind) -> None:
        while True:
            with self._cond:
                if self._closing:
                    return
            claim = self._ctx.state.queue.claim_next(network, self._owner)
            if claim is None:
                return
            attempt = Attempt(self._ctx, claim)
            with self._cond:
                self._attempts[claim.attempt_id] = attempt
                if self._closing:
                    attempt.stop("shutdown")
            attempt.thread.start()

    def _sleep_for(self) -> float:
        config = self._ctx.config
        if self.active:
            return config.monitor_interval_sec
        due = [j.not_before for j in self._ctx.state.queue.jobs([JobState.QUEUED]) if j.not_before is not None]
        if not due:
            return config.idle_interval_sec
        try:
            until = (datetime.fromisoformat(min(due)) - self._ctx.wall()).total_seconds()
        except ValueError:
            return config.idle_interval_sec
        # never zero: a due job that cannot be claimed yet must not turn the loop into a busy wait
        return max(min(until, config.idle_interval_sec), config.monitor_interval_sec)
