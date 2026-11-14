package pl.altani.spike.bridge

import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.take
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.async
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class EventsPumpTest {
    private val requests = Channel<Pair<String, JSONObject>>(Channel.UNLIMITED)
    private val replies = Channel<BridgeResult>(Channel.UNLIMITED)

    private fun TestScope.pump() =
        EventsPump(
            source = { method, args ->
                requests.send(method to args)
                replies.receive()
            },
            scope = backgroundScope,
            retryDelayMs = 2_000,
        )

    private fun ok(data: JSONObject) = BridgeResult.Ok(data)

    private fun head(epoch: String, seq: Long) = ok(JSONObject().put("epoch", epoch).put("seq", seq))

    private fun next(epoch: String, nextSeq: Long, vararg seqs: Long, resync: Boolean = false) =
        ok(
            JSONObject()
                .put("epoch", epoch)
                .put("events", JSONArray(seqs.map { JSONObject().put("seq", it).put("type", "job.changed") }))
                .put("next_seq", nextSeq)
                .put("resync", resync),
        )

    private suspend fun expect(method: String): JSONObject {
        val (actual, args) = requests.receive()
        assertEquals(method, actual)
        return args
    }

    @Test
    fun `events arrive in order and the cursor follows next_seq`() = runTest {
        val pump = pump()
        val events = async(start = CoroutineStart.UNDISPATCHED) { pump.events.take(2).toList() }
        replies.send(head("e", 5))
        replies.send(next("e", 7, 5, 6))
        pump.start()

        expect("events_head")
        val first = expect("events_next")
        assertEquals(5, first.getJSONObject("cursor").getLong("seq"))
        assertEquals(25_000, first.getInt("wait_ms"))
        val second = expect("events_next")

        assertEquals(7, second.getJSONObject("cursor").getLong("seq"))
        assertEquals(listOf(5L, 6L), events.await().map { it.getLong("seq") })
        assertEquals(7, pump.state.value.nextSeq)
    }

    @Test
    fun `resync or another epoch starts over from a new head and signals the holders`() = runTest {
        val pump = pump()
        // conflated: a holder only needs to know that its snapshots are stale
        val resynced = async(start = CoroutineStart.UNDISPATCHED) { pump.resync.first() }
        replies.send(head("e", 1))
        replies.send(next("e", 4, resync = true))
        replies.send(head("e", 4))
        replies.send(next("other", 1))
        replies.send(head("other", 1))
        pump.start()

        listOf("events_head", "events_next", "events_head", "events_next", "events_head").forEach { expect(it) }
        val after = expect("events_next")

        resynced.await()
        assertEquals("other", after.getJSONObject("cursor").getString("epoch"))
        assertEquals(3, pump.state.value.heads)
    }

    @Test
    fun `an unavailable bridge is retried after a pause`() = runTest {
        val pump = pump()
        replies.send(BridgeResult.Failure("network.offline", "unavailable", true, JSONObject()))
        replies.send(head("e", 1))
        pump.start()

        expect("events_head")
        pump.state.first { it.lastError == "network.offline" }
        val retriedAt = testScheduler.currentTime
        expect("events_head")

        assertEquals(2_000, testScheduler.currentTime - retriedAt)
        expect("events_next")
        assertNull(pump.state.value.lastError)
    }

    @Test
    fun `a closed engine or a cancelled poll stops the pump`() = runTest {
        val pump = pump()
        replies.send(head("e", 1))
        replies.send(BridgeResult.Failure("cancelled", "cancelled", false, JSONObject()))
        pump.start()

        expect("events_head")
        expect("events_next")
        val stopped = pump.state.first { !it.running && it.heads == 1 }

        assertEquals("cancelled", stopped.lastError)
        assertFalse(stopped.running)
    }

    @Test
    fun `stop cancels the waiting poll`() = runTest {
        val pump = pump()
        replies.send(head("e", 1))
        pump.start()
        expect("events_head")
        expect("events_next")

        pump.stop()

        assertFalse(pump.state.value.running)
    }
}
