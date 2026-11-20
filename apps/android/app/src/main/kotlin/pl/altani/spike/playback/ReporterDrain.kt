package pl.altani.spike.playback

import kotlinx.coroutines.withTimeoutOrNull

/** Closed reporters whose last writes may still be on their way; a new `playback_begin` waits for them, within a limit. */
class ReporterDrain(private val timeoutMs: Long = DRAIN_MS) {
    private val pending = mutableListOf<PositionReporter>()

    val size: Int
        get() = pending.count { !it.finished }

    fun add(reporter: PositionReporter) {
        pending += reporter
    }

    /** True when every earlier reporter finished; false after the timeout, when the next begin may still overtake a write. */
    suspend fun await(): Boolean {
        pending.removeAll { it.finished }
        val waiting = pending.toList()
        if (waiting.isEmpty()) return true
        val done = withTimeoutOrNull(timeoutMs) { waiting.forEach { it.join() } } != null
        pending.removeAll { it.finished }
        return done
    }

    companion object {
        const val DRAIN_MS = 2_000L
    }
}
