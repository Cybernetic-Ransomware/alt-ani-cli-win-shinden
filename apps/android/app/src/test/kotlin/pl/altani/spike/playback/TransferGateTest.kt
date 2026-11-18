package pl.altani.spike.playback

import java.io.InterruptedIOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TransferGateTest {
    private fun waiter(gate: TransferGate, started: CountDownLatch, outcome: AtomicReference<Any>): Thread =
        Thread {
            started.countDown()
            outcome.set(runCatching { gate.awaitOpen() }.exceptionOrNull() ?: "opened")
        }.apply { start() }

    @Test
    fun aWaiterGoesOnOnlyOnceTheGateOpens() {
        val gate = TransferGate()
        gate.close()
        val started = CountDownLatch(1)
        val outcome = AtomicReference<Any>()
        val thread = waiter(gate, started, outcome)
        assertTrue(started.await(5, TimeUnit.SECONDS))
        thread.join(300)
        assertTrue("still waiting while closed", thread.isAlive)
        gate.open()
        thread.join(5_000)
        assertEquals("opened", outcome.get())
    }

    @Test
    fun anInterruptEndsTheWaitAsAnIoErrorAndKeepsTheFlag() {
        val gate = TransferGate()
        gate.close()
        val started = CountDownLatch(1)
        val outcome = AtomicReference<Any>()
        val flag = AtomicReference<Boolean>()
        val thread = Thread {
            started.countDown()
            outcome.set(runCatching { gate.awaitOpen() }.exceptionOrNull())
            flag.set(Thread.currentThread().isInterrupted)
        }.apply { start() }
        assertTrue(started.await(5, TimeUnit.SECONDS))
        thread.interrupt()
        thread.join(5_000)
        assertTrue(outcome.get() is InterruptedIOException)
        assertTrue(flag.get())
        assertFalse(gate.isOpen)
    }

    @Test
    fun stampsTellClosedPeriodsApart() {
        val gate = TransferGate()
        assertEquals(TransferGate.Stamp(true, 0, 0), gate.stamp().copy(nanos = 0))
        val first = gate.close()
        assertEquals(first.closures, gate.close().closures)
        gate.open()
        val second = gate.close()
        assertEquals(listOf(1, 2), listOf(first.closures, second.closures))
        assertTrue(second.nanos > first.nanos)
    }
}
