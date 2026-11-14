package pl.altani.spike.bridge

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.FakeBridge.Companion.awaitOrFail

class BridgeRuntimeTest {
    private val bridge = FakeBridge()

    private fun runtime(callThreads: Int = 2) =
        BridgeRuntime(
            endpointFactory = { bridge },
            engineArgs = { JSONObject().put("cache_dir", "cache").put("history_path", "state.db") },
            callThreads = callThreads,
        )

    private fun test(body: suspend CoroutineScope.() -> Unit) = runBlocking { withTimeout(15_000) { body() } }

    @Test
    fun `concurrent first calls create one engine and carry its id`() = test {
        val runtime = runtime()
        val replies = (1..20).map { async(Dispatchers.Default) { runtime.call("bridge_info") } }.awaitAll()

        assertTrue(replies.all { it is BridgeResult.Ok })
        assertEquals(1, bridge.engines.get())
        assertEquals(1, runtime.engineCreations)
        assertTrue(bridge.payloads.all { it.getString("engine_id") == "engine-1" })
        assertTrue(runtime.shutdown(5_000).ended)
    }

    @Test
    fun `a failed engine creation is reported and retried by the next call`() = test {
        var first = true
        bridge.createEngineReply = {
            if (first) {
                first = false
                FakeBridge.failure("storage.unavailable", "storage")
            } else {
                FakeBridge.ok(JSONObject().put("engine_id", "engine-1"))
            }
        }
        val runtime = runtime()

        assertEquals("storage.unavailable", (runtime.call("bridge_info") as BridgeResult.Failure).code)
        assertTrue(runtime.call("bridge_info") is BridgeResult.Ok)
        assertEquals(1, runtime.engineCreations)
    }

    @Test
    fun `shutdown ends a cooperating operation and releases the runtime`() = test {
        val entered = CountDownLatch(1)
        bridge.handler = { _, cancelled ->
            entered.countDown()
            if (cancelled.await(30, TimeUnit.SECONDS)) FakeBridge.CANCELLED else FakeBridge.ok()
        }
        val runtime = runtime()
        val poll = async(Dispatchers.Default) { runtime.call("events_next") }
        entered.awaitOrFail("poll running")

        val report = runtime.shutdown(5_000)

        assertTrue(report.ended)
        assertTrue(runtime.released)
        assertTrue(poll.await().isCancelled)
        assertEquals(BridgeRuntime.CLOSED, runtime.call("bridge_info"))
    }

    @Test
    fun `shutdown with stuck I O keeps every resource until the operation returns`() = test {
        val entered = CountDownLatch(1)
        val unstick = CountDownLatch(1)
        bridge.handler = { _, _ ->
            entered.countDown()
            // ignores the cancel, like a socket read with no timeout
            unstick.await()
            FakeBridge.ok()
        }
        val runtime = runtime()
        val stuck = async(Dispatchers.Default) { runtime.call("library_export", mutation = true) }
        entered.awaitOrFail("stuck call")

        val started = System.nanoTime()
        val first = runtime.shutdown(300)
        val elapsedMs = (System.nanoTime() - started) / 1_000_000

        assertFalse(first.ended)
        assertTrue("answered within timeout + 1 s, took $elapsedMs ms", elapsedMs < 1_300)
        assertFalse(runtime.released)
        assertEquals(1, first.activeCalls)
        assertEquals("no new calls while closing", BridgeRuntime.CLOSED, runtime.call("bridge_info"))

        unstick.countDown()
        assertTrue(stuck.await() is BridgeResult.Ok)
        val second = runtime.shutdown(5_000)

        assertTrue(second.ended)
        assertTrue(runtime.released)
        assertEquals(0, second.activeCalls)
        assertTrue("repeatable once released", runtime.shutdown(5_000).ended)
        assertEquals(2, bridge.shutdowns.get())
    }

    @Test
    fun `the long poll runs on its own thread while every call thread is busy`() = test {
        val slowEntered = CountDownLatch(1)
        val releaseSlow = CountDownLatch(1)
        val pollEntered = CountDownLatch(1)
        var pollThread = ""
        bridge.handler = { method, cancelled ->
            when (method) {
                "slow" -> {
                    slowEntered.countDown()
                    releaseSlow.await()
                    FakeBridge.ok()
                }
                "events_head" -> FakeBridge.ok(JSONObject().put("epoch", "e").put("seq", 1))
                else -> {
                    pollThread = Thread.currentThread().name
                    pollEntered.countDown()
                    if (cancelled.await(30, TimeUnit.SECONDS)) FakeBridge.CANCELLED else FakeBridge.ok()
                }
            }
        }
        val runtime = runtime(callThreads = 1)
        val slow = async(Dispatchers.Default) { runtime.call("slow") }
        slowEntered.awaitOrFail("call thread taken")

        runtime.events.start()
        pollEntered.awaitOrFail("poll despite a busy pool")
        assertTrue(pollThread, pollThread.startsWith("bridge-events"))

        runtime.events.stop()
        bridge.cancelSeen.awaitOrFail("poll cancelled")
        runtime.tracker.active.first { it == 1 }
        releaseSlow.countDown()
        slow.await()
        assertTrue(runtime.tracker.awaitIdle(5_000))
        assertTrue(runtime.shutdown(5_000).ended)
    }
}
