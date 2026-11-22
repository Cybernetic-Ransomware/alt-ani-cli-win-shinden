package pl.altani.spike.playback

import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class PlaybackArbiterTest {
    private enum class Mode { CONTROLLED, CATALOG }

    @Test
    fun `switching modes releases the previous one, a restart in the same mode does not`() = runTest {
        val released = mutableListOf<Mode>()
        val arbiter = PlaybackArbiter<Mode>(backgroundScope) { released += it }

        arbiter.launch(Mode.CONTROLLED) {}
        arbiter.launch(Mode.CONTROLLED) {}
        arbiter.launch(Mode.CATALOG) {}
        arbiter.launch(Mode.CONTROLLED) {}

        assertEquals(listOf(Mode.CONTROLLED, Mode.CATALOG), released)
        assertEquals(Mode.CONTROLLED, arbiter.mode.value)
    }

    @Test
    fun `a new flow cancels the previous one`() = runTest {
        val arbiter = PlaybackArbiter<Mode>(backgroundScope) {}
        val never = CompletableDeferred<Unit>()
        val first = arbiter.launch(Mode.CONTROLLED) { never.await() }
        runCurrent()

        arbiter.launch(Mode.CATALOG) {}
        runCurrent()

        assertTrue(first.isCancelled)
    }

    @Test
    fun `a reply that outlives its cancellation starts nothing`() = runTest {
        val arbiter = PlaybackArbiter<Mode>(backgroundScope) {}
        val started = mutableListOf<Mode>()
        var reply: Continuation<Unit>? = null
        arbiter.launch(Mode.CONTROLLED) { ticket ->
            // not cancellable, like a bridge reply already on its way back
            suspendCoroutine { reply = it }
            if (ticket.current) started += ticket.mode
        }
        runCurrent()
        arbiter.launch(Mode.CATALOG) { ticket -> if (ticket.current) started += ticket.mode }
        runCurrent()

        reply!!.resume(Unit)
        runCurrent()

        assertEquals(listOf(Mode.CATALOG), started)
    }
}
