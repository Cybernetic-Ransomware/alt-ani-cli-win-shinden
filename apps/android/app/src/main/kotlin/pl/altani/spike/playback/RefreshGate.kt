package pl.altani.spike.playback

/** One `stream_refresh` at a time, and at most one per [windowMs]: a URL that keeps expiring stops playback. */
class RefreshGate(
    private val windowMs: Long = 30_000,
    private val nowMs: () -> Long = { System.nanoTime() / 1_000_000 },
) {
    enum class Decision {
        START,

        /** A refresh is already running; it replaces the failing source, so this error needs nothing. */
        BUSY,
        LIMIT,
    }

    private var running = false
    private var lastStart: Long? = null

    @Synchronized
    fun request(): Decision {
        if (running) return Decision.BUSY
        val now = nowMs()
        val last = lastStart
        if (last != null && now - last < windowMs) return Decision.LIMIT
        running = true
        lastStart = now
        return Decision.START
    }

    @Synchronized
    fun finished() {
        running = false
    }
}
