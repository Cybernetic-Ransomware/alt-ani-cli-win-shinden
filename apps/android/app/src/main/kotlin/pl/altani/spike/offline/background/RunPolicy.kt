package pl.altani.spike.offline.background

enum class RunPath { FOREGROUND_SERVICE, USER_INITIATED_JOB }

enum class EndReason { DONE, NEEDS_USER, NETWORK_WAIT }

sealed interface RunDecision {
    /** [recheckAtMs]: when a limit runs out and the session must be judged again on a fresh read. */
    data class Keep(val wakeLock: Boolean, val recheckAtMs: Long? = null) : RunDecision

    data class End(val reason: EndReason, val reschedule: Boolean) : RunDecision
}

/** Whether the Android component should stay; never which job runs, that is the engine's scheduler. */
object RunPolicy {
    const val NETWORK_GRACE_MS = 10 * 60_000L
    const val SETTLE_MS = 30_000L
    const val CLAIM_WINDOW_MS = 30_000L
    const val RESCHEDULE_WINDOW_MS = 24 * 60 * 60_000L

    /** [claimSinceMs]: since when a due job waits with no transfer; [rescheduleAllowed]: 24 h window and network scope. */
    fun decide(
        summary: RunSummary,
        phaseSinceMs: Long,
        claimSinceMs: Long?,
        nowMs: Long,
        path: RunPath,
        rescheduleAllowed: Boolean,
    ): RunDecision =
        when (summary.phase) {
            RunPhase.WORKING -> {
                // a due job is normally claimed within one scheduler pass; the lock never outlives that window without I/O
                val claiming = summary.activeIo == 0 && claimSinceMs != null && nowMs - claimSinceMs < CLAIM_WINDOW_MS
                RunDecision.Keep(
                    wakeLock = summary.activeIo > 0 || claiming,
                    recheckAtMs = if (claiming) claimSinceMs!! + CLAIM_WINDOW_MS else null,
                )
            }
            RunPhase.DEFERRED -> RunDecision.Keep(wakeLock = false)
            RunPhase.WAITING_NETWORK ->
                if (nowMs - phaseSinceMs >= NETWORK_GRACE_MS) {
                    RunDecision.End(EndReason.NETWORK_WAIT, reschedule = path == RunPath.USER_INITIATED_JOB && rescheduleAllowed)
                } else {
                    RunDecision.Keep(wakeLock = false, recheckAtMs = phaseSinceMs + NETWORK_GRACE_MS)
                }
            RunPhase.NEEDS_USER ->
                if (nowMs - phaseSinceMs >= SETTLE_MS) {
                    RunDecision.End(EndReason.NEEDS_USER, reschedule = false)
                } else {
                    RunDecision.Keep(wakeLock = false, recheckAtMs = phaseSinceMs + SETTLE_MS)
                }
            RunPhase.IDLE -> RunDecision.End(EndReason.DONE, reschedule = false)
        }

    /** The window bounds automatic restarts only; a session that runs is never cut short by it. */
    fun withinWindow(initiatedAtMs: Long, nowMs: Long): Boolean = nowMs - initiatedAtMs < RESCHEDULE_WINDOW_MS
}
