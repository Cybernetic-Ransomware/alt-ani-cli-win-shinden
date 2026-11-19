package pl.altani.spike.bridge

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class NetworkSyncTest {
    private val sent = mutableListOf<String>()
    private var requested = true
    private var reply: BridgeResult = BridgeResult.Ok(JSONObject())
    private var answer: CompletableDeferred<BridgeResult>? = null
    private val refused = BridgeResult.Failure("engine_closed", "closed", false, JSONObject())

    private fun TestScope.sync(initial: String = "wifi") =
        NetworkSync(initial, backgroundScope, { requested }, { kind ->
            sent += kind
            answer?.await() ?: reply
        }, retryMs = 1_000, maxRetryMs = 4_000, callTimeoutMs = 10_000)

    @Test
    fun `the initial kind and repeats of the known one are never sent`() = runTest {
        val sync = sync()
        runCurrent()
        sync.update("wifi")
        runCurrent()
        assertEquals(emptyList<String>(), sent)
    }

    @Test
    fun `a change reaches a requested engine once`() = runTest {
        val sync = sync()
        runCurrent()
        sync.update("cellular")
        runCurrent()
        sync.update("cellular")
        runCurrent()
        assertEquals(listOf("cellular"), sent)
        assertEquals("cellular", sync.kind.value)
    }

    @Test
    fun `without an engine the change only updates its creation arguments`() = runTest {
        requested = false
        val sync = sync()
        runCurrent()
        sync.update("offline")
        runCurrent()
        assertEquals(emptyList<String>(), sent)
        assertEquals("offline", sync.kind.value)

        requested = true
        sync.update("wifi")
        runCurrent()
        assertEquals(listOf("wifi"), sent)
    }

    @Test
    fun `a burst is sent as its last kind`() = runTest {
        val sync = sync()
        sync.update("offline")
        sync.update("cellular")
        sync.update("offline")
        runCurrent()
        assertEquals(listOf("offline"), sent)
    }

    @Test
    fun `a refused change is sent again when the kind comes back`() = runTest {
        val sync = sync()
        runCurrent()
        reply = refused
        sync.update("cellular")
        runCurrent()
        reply = BridgeResult.Ok(JSONObject())
        sync.update("offline")
        runCurrent()
        sync.update("cellular")
        runCurrent()
        assertEquals(listOf("cellular", "offline", "cellular"), sent)
    }

    @Test
    fun `a failed change is retried with a growing delay until the engine takes it`() = runTest {
        val sync = sync()
        runCurrent()
        reply = refused
        sync.update("cellular")
        runCurrent()
        advanceTimeBy(1_001)
        assertEquals(listOf("cellular", "cellular"), sent)
        advanceTimeBy(1_500)
        assertEquals("the second wait is 2 s", 2, sent.size)
        advanceTimeBy(600)
        assertEquals(3, sent.size)
        reply = BridgeResult.Ok(JSONObject())
        advanceTimeBy(4_001)
        assertEquals(4, sent.size)
        advanceTimeBy(60_000)
        assertEquals("no more calls once taken", 4, sent.size)
    }

    @Test
    fun `a newer kind cuts the retry wait short`() = runTest {
        val sync = sync()
        runCurrent()
        reply = refused
        sync.update("cellular")
        runCurrent()
        reply = BridgeResult.Ok(JSONObject())
        sync.update("offline")
        runCurrent()
        assertEquals(listOf("cellular", "offline"), sent)
        advanceTimeBy(60_000)
        assertEquals(2, sent.size)
    }

    @Test
    fun `after a failure the old kind is sent again, the engine may have taken the failed one`() = runTest {
        val sync = sync()
        runCurrent()
        reply = refused
        sync.update("cellular")
        runCurrent()
        reply = BridgeResult.Ok(JSONObject())
        sync.update("wifi")
        runCurrent()
        assertEquals(listOf("cellular", "wifi"), sent)
    }

    @Test
    fun `a call that never answers times out and is retried with the latest kind`() = runTest {
        val sync = sync()
        runCurrent()
        answer = CompletableDeferred()
        sync.update("cellular")
        runCurrent()
        sync.update("offline")
        runCurrent()
        assertEquals("the stuck call is still out", listOf("cellular"), sent)
        answer = null
        advanceTimeBy(10_001)
        assertEquals(listOf("cellular", "offline"), sent)
    }
}
