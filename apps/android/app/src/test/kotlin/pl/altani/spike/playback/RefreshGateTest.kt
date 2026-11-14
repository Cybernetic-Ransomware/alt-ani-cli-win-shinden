package pl.altani.spike.playback

import org.junit.Assert.assertEquals
import org.junit.Test

class RefreshGateTest {
    private var now = 0L
    private val gate = RefreshGate(windowMs = 30_000, nowMs = { now })

    @Test
    fun `errors while a refresh runs start nothing more`() {
        assertEquals(RefreshGate.Decision.START, gate.request())
        repeat(5) { assertEquals(RefreshGate.Decision.BUSY, gate.request()) }
    }

    @Test
    fun `a second expiry inside the window stops instead of refreshing again`() {
        gate.request()
        gate.finished()
        now = 29_999
        assertEquals(RefreshGate.Decision.LIMIT, gate.request())
    }

    @Test
    fun `after the window another refresh is allowed`() {
        gate.request()
        gate.finished()
        now = 30_000
        assertEquals(RefreshGate.Decision.START, gate.request())
    }
}
