package pl.altani.spike.offline.background

import org.junit.Assert.assertEquals
import org.junit.Test
import pl.altani.spike.offline.background.RunPolicy.CLAIM_WINDOW_MS
import pl.altani.spike.offline.background.RunPolicy.NETWORK_GRACE_MS
import pl.altani.spike.offline.background.RunPolicy.RESCHEDULE_WINDOW_MS
import pl.altani.spike.offline.background.RunPolicy.SETTLE_MS

class RunPolicyTest {
    private val fgs = RunPath.FOREGROUND_SERVICE
    private val uidt = RunPath.USER_INITIATED_JOB

    private fun decide(
        summary: RunSummary,
        since: Long = 0,
        now: Long = 0,
        path: RunPath = fgs,
        claimSince: Long? = null,
        reschedule: Boolean = true,
    ) = RunPolicy.decide(summary, since, claimSince, now, path, reschedule)

    @Test
    fun `transfers hold the wake lock and never end the session`() {
        val working = RunSummary(activeIo = 1, waitingNetwork = 3, needsUser = 2)
        assertEquals(RunDecision.Keep(wakeLock = true), decide(working, now = RESCHEDULE_WINDOW_MS * 2))
    }

    @Test
    fun `a due job without a transfer holds the lock for the claim window only`() {
        val due = RunSummary(readyNow = 1)
        assertEquals(RunDecision.Keep(true, 1_000 + CLAIM_WINDOW_MS), decide(due, now = 1_000, claimSince = 1_000))
        assertEquals(RunDecision.Keep(false, null), decide(due, now = 1_000 + CLAIM_WINDOW_MS, claimSince = 1_000))
    }

    @Test
    fun `a deferred job keeps the session without the lock`() {
        assertEquals(RunDecision.Keep(wakeLock = false), decide(RunSummary(deferred = 1), now = NETWORK_GRACE_MS * 10))
    }

    @Test
    fun `a network wait ends after the grace, restarted by the system only for a user-initiated job`() {
        val waiting = RunSummary(waitingNetwork = 1)
        assertEquals(RunDecision.Keep(false, 5 + NETWORK_GRACE_MS), decide(waiting, since = 5, now = NETWORK_GRACE_MS))
        assertEquals(RunDecision.End(EndReason.NETWORK_WAIT, false), decide(waiting, since = 5, now = 5 + NETWORK_GRACE_MS))
        assertEquals(RunDecision.End(EndReason.NETWORK_WAIT, true), decide(waiting, now = NETWORK_GRACE_MS, path = uidt))
        assertEquals(
            RunDecision.End(EndReason.NETWORK_WAIT, false),
            decide(waiting, now = NETWORK_GRACE_MS, path = uidt, reschedule = false),
        )
    }

    @Test
    fun `waits for the user settle for thirty seconds and are never restarted`() {
        val stuck = RunSummary(needsUser = 2)
        assertEquals(RunDecision.Keep(false, 100 + SETTLE_MS), decide(stuck, since = 100, now = 100 + SETTLE_MS - 1))
        assertEquals(RunDecision.End(EndReason.NEEDS_USER, false), decide(stuck, since = 100, now = 100 + SETTLE_MS, path = uidt))
    }

    @Test
    fun `nothing left ends at once`() {
        assertEquals(RunDecision.End(EndReason.DONE, false), decide(RunSummary(), path = uidt))
    }

    @Test
    fun `the restart window is twenty four hours from the user's decision`() {
        assertEquals(true, RunPolicy.withinWindow(1_000, 1_000 + RESCHEDULE_WINDOW_MS - 1))
        assertEquals(false, RunPolicy.withinWindow(1_000, 1_000 + RESCHEDULE_WINDOW_MS))
    }
}
