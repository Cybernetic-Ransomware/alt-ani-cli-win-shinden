package pl.altani.spike.bridge

import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.CyclicBarrier
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.FakeBridge.Companion.awaitOrFail

class BridgeClientTest {
    private val bridge = FakeBridge()
    private val executors = mutableListOf<ExecutorService>()
    private val scope = CoroutineScope(SupervisorJob())
    private val cancelExecutor = pool(1)
    private val late = ConcurrentLinkedQueue<LateResult>()
    private val lateCount = MutableStateFlow(0)

    private fun pool(threads: Int): ExecutorService = Executors.newFixedThreadPool(threads).also { executors += it }

    private fun client(callThreads: Int = 2): BridgeClient {
        val client = BridgeClient(bridge, scope, pool(callThreads).asCoroutineDispatcher(), cancelExecutor)
        scope.launch(start = CoroutineStart.UNDISPATCHED) { client.tracker.lateResults.collect {
                late += it
                lateCount.value = late.size
            }
        }
        return client
    }

    @After
    fun tearDown() {
        scope.cancel()
        executors.forEach { it.shutdownNow() }
    }

    private fun test(body: suspend CoroutineScope.() -> Unit) = runBlocking { withTimeout(15_000) { body() } }

    private suspend fun BridgeClient.idle() = assertTrue("tracker not idle", tracker.awaitIdle(5_000))

    private suspend fun awaitLate(count: Int) {
        lateCount.first { it >= count }
    }

    @Test
    fun `a call cancelled while queued never reaches Python`() = test {
        val callPool = pool(1)
        val occupied = CountDownLatch(1)
        val release = CountDownLatch(1)
        callPool.execute {
            occupied.countDown()
            release.await()
        }
        occupied.awaitOrFail("busy pool")
        val client = BridgeClient(bridge, scope, callPool.asCoroutineDispatcher(), cancelExecutor)
        val caller = launch(Dispatchers.Default) { client.call("events_next") }
        client.tracker.active.first { it == 1 }
        caller.cancelAndJoin()
        release.countDown()
        callPool.submit {}.get(5, TimeUnit.SECONDS)

        client.idle()
        assertTrue(caller.isCancelled)
        assertTrue(bridge.invoked.isEmpty())
        assertTrue("no bridge cancel needed", bridge.cancels.isEmpty())
    }

    @Test
    fun `a cancel that overtakes registration ends the call before its handler`() = test {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        bridge.beforeRegister = {
            entered.countDown()
            release.await()
        }
        val client = client()
        val caller = launch(Dispatchers.Default) { client.call("playback_begin", mutation = true) }
        entered.awaitOrFail("call started")
        caller.cancelAndJoin()
        bridge.cancelSeen.awaitOrFail("cancel sent")
        release.countDown()

        client.idle()
        awaitLate(1)
        assertTrue("handler never ran", bridge.invoked.isEmpty())
        val result = late.single()
        assertTrue(result.result.isCancelled)
        assertFalse("a refused call needs no refresh", result.needsRefresh)
    }

    @Test
    fun `cancelling a running long poll ends it within a second without a free pool slot`() = test {
        val entered = CountDownLatch(1)
        bridge.handler = { _, cancelled ->
            entered.countDown()
            if (cancelled.await(30, TimeUnit.SECONDS)) FakeBridge.CANCELLED else FakeBridge.ok()
        }
        // the only call thread is held by the poll itself, so the cancel has to travel another way
        val client = client(callThreads = 1)
        val caller = launch(Dispatchers.Default) { client.call("events_next", JSONObject().put("wait_ms", 30_000)) }
        entered.awaitOrFail("poll running")
        val started = System.nanoTime()
        caller.cancelAndJoin()
        client.idle()
        val elapsedMs = (System.nanoTime() - started) / 1_000_000

        assertTrue("took $elapsedMs ms", elapsedMs < 1_000)
        awaitLate(1)
        assertEquals(1, bridge.cancels.size)
        assertTrue(late.single().result.isCancelled)
    }

    @Test
    fun `a mutation cancelled after its write reports the write as a late result`() = test {
        val committed = CountDownLatch(1)
        val finish = CountDownLatch(1)
        val writes = AtomicInteger()
        bridge.handler = { _, _ ->
            val revision = writes.incrementAndGet()
            committed.countDown()
            // past the commit point the bridge ignores the cancel and reports its outcome
            finish.await()
            FakeBridge.ok(JSONObject().put("revision", revision))
        }
        val client = client()
        val caller = launch(Dispatchers.Default) { client.call("watch_set_watched", mutation = true) }
        committed.awaitOrFail("write")
        caller.cancelAndJoin()
        bridge.cancelSeen.awaitOrFail("cancel sent")
        finish.countDown()

        client.idle()
        awaitLate(1)
        assertTrue(caller.isCancelled)
        assertEquals(1, writes.get())
        val result = late.single()
        assertTrue(result.needsRefresh)
        assertEquals(1, (result.result as BridgeResult.Ok).data.getInt("revision"))
        assertEquals("never repeated", 1, bridge.invoked.size)
    }

    @Test
    fun `a result ready while the caller is cancelled is accounted exactly once`() = test {
        val client = client()
        val delivered = mutableSetOf<String>()
        val cancelledCallers = mutableSetOf<String>()
        repeat(200) { round ->
            val barrier = CyclicBarrier(2)
            bridge.handler = { _, _ ->
                barrier.await(5, TimeUnit.SECONDS)
                FakeBridge.ok(JSONObject().put("round", round))
            }
            var received: BridgeResult? = null
            val caller = async(Dispatchers.Default) {
                // recorded here: the job can still be cancelled after call() has handed the result over
                received = client.call("playback_position", mutation = true)
            }
            barrier.await(5, TimeUnit.SECONDS)
            caller.cancel()
            try {
                caller.await()
            } catch (e: CancellationException) {
                // the outcome is decided by what call() returned, not by how the job ended
            }
            when (val result = received) {
                is BridgeResult.Ok -> delivered += "${result.data.getInt("round")}"
                else -> cancelledCallers += "$round"
            }
            client.idle()
        }
        awaitLate(cancelledCallers.size)

        val lateRounds = late.map { (it.result as BridgeResult.Ok).data.getInt("round").toString() }
        assertEquals("one late result per cancelled caller", cancelledCallers, lateRounds.toSet())
        assertEquals(lateRounds.size, lateRounds.toSet().size)
        assertTrue("never both", delivered.intersect(cancelledCallers).isEmpty())
        assertEquals(200, delivered.size + cancelledCallers.size)
    }

    @Test
    fun `a delivered result is not repeated as a late one`() = test {
        val client = client()
        val reply = client.call("bridge_info")
        client.idle()
        assertTrue(reply is BridgeResult.Ok)
        assertTrue(late.isEmpty())
        assertTrue(bridge.cancels.isEmpty())
    }

    @Test
    fun `the op_id reaches the bridge with the arguments`() = test {
        var payload: JSONObject? = null
        val seen = CountDownLatch(1)
        val endpoint = object : BridgeEndpoint by bridge {
            override fun call(method: String, payloadJson: String): String {
                payload = JSONObject(payloadJson)
                seen.countDown()
                return FakeBridge.ok()
            }
        }
        val client = BridgeClient(endpoint, scope, pool(1).asCoroutineDispatcher(), cancelExecutor, newOpId = { "op-1" })
        client.call("watch_get", JSONObject().put("series_id", "s"))
        seen.awaitOrFail("call")
        val sent = payload!!
        assertEquals("op-1", sent.getString("op_id"))
        assertEquals("s", sent.getString("series_id"))
    }
}
