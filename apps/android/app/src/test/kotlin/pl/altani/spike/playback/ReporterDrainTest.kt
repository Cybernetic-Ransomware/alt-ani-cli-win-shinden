package pl.altani.spike.playback

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.BridgeResult

/** The controller's stop → start sequence on real reporters: the old viewing's writes reach the bridge before the new begin. */
@OptIn(ExperimentalCoroutinesApi::class)
class ReporterDrainTest {
    private val sent = mutableListOf<String>()
    private val held = mutableMapOf<String, CompletableDeferred<Unit>>()

    private fun session(id: String) = JSONObject().put("id", id).put("series_id", "7").put("number", 1)

    private suspend fun call(method: String, args: JSONObject): BridgeResult {
        val id = args.getJSONObject("playback_session").getString("id")
        held["$method/$id"]?.await()
        sent += "$method/$id"
        return BridgeResult.Ok(JSONObject().put("accepted", true))
    }

    private fun TestScope.reporter(id: String) = PositionReporter(backgroundScope, session(id), ::call)

    /** What stopSession() does: the real position, then close, then the reporter goes to the drain. */
    private fun stop(reporter: PositionReporter, drain: ReporterDrain, positionMs: Long) {
        reporter.explicit(positionMs, 1_440_000)
        reporter.close()
        drain.add(reporter)
    }

    /** What start() does after its stop: wait, then begin and prepare unless a later start came first. */
    private fun TestScope.start(drain: ReporterDrain, generation: () -> Int, id: String, timedOut: MutableList<Boolean>? = null) {
        val started = generation()
        backgroundScope.launch {
            val done = drain.await()
            timedOut?.add(!done)
            if (started != generation()) return@launch
            sent += "playback_begin/$id"
            sent += "playback_prepare/$id"
        }
    }

    @Test
    fun `a source change waits for the old position and close before begin and prepare`() = runTest {
        val drain = ReporterDrain()
        val old = reporter("a")
        old.periodic(50_000, 1_440_000)
        held["playback_position/a"] = CompletableDeferred()
        stop(old, drain, 61_000)
        start(drain, { 1 }, "b")
        runCurrent()
        assertTrue("begin waits for the slow write", sent.none { it.startsWith("playback_begin") })

        held.getValue("playback_position/a").complete(Unit)
        runCurrent()
        assertEquals(
            listOf("playback_position/a", "playback_position/a", "playback_close/a", "playback_begin/b", "playback_prepare/b"),
            sent,
        )
        assertEquals(0, drain.size)
    }

    @Test
    fun `a close followed by the start's own stop keeps the reporter to wait for`() = runTest {
        val drain = ReporterDrain()
        var current: PositionReporter? = reporter("a")
        held["playback_close/a"] = CompletableDeferred()
        // playCatalog closes first, then start() stops again: the second stop finds no reporter and must not drop the first
        current?.let { stop(it, drain, 30_000) }
        current = null
        current?.let { stop(it, drain, 0) }
        start(drain, { 1 }, "a2")
        runCurrent()
        assertEquals(1, drain.size)
        assertEquals(listOf("playback_position/a"), sent)

        held.getValue("playback_close/a").complete(Unit)
        runCurrent()
        assertEquals(listOf("playback_position/a", "playback_close/a", "playback_begin/a2", "playback_prepare/a2"), sent)
    }

    @Test
    fun `quick repeated starts begin only the last one, after every earlier viewing closed`() = runTest {
        val drain = ReporterDrain()
        var generation = 0
        held["playback_close/a"] = CompletableDeferred()
        stop(reporter("a"), drain, 10_000)
        generation++
        start(drain, { generation }, "b")
        runCurrent()
        // B is superseded while it waits; C's stop has nothing new to close
        generation++
        start(drain, { generation }, "c")
        runCurrent()

        held.getValue("playback_close/a").complete(Unit)
        runCurrent()
        assertEquals(listOf("playback_position/a", "playback_close/a", "playback_begin/c", "playback_prepare/c"), sent)
    }

    @Test
    fun `several stopped viewings are all drained`() = runTest {
        val drain = ReporterDrain()
        held["playback_close/a"] = CompletableDeferred()
        held["playback_close/b"] = CompletableDeferred()
        stop(reporter("a"), drain, 1_000)
        stop(reporter("b"), drain, 2_000)
        start(drain, { 1 }, "c")
        runCurrent()
        held.getValue("playback_close/b").complete(Unit)
        runCurrent()
        assertTrue(sent.none { it.startsWith("playback_begin") })
        held.getValue("playback_close/a").complete(Unit)
        runCurrent()
        assertEquals("playback_begin/c", sent[sent.size - 2])
    }

    @Test
    fun `a bridge that never answers delays the begin by the limit only, and the drain says so`() = runTest {
        val drain = ReporterDrain(timeoutMs = 2_000)
        val timedOut = mutableListOf<Boolean>()
        held["playback_position/a"] = CompletableDeferred()
        stop(reporter("a"), drain, 61_000)
        start(drain, { 1 }, "b", timedOut)
        advanceTimeBy(1_999)
        runCurrent()
        assertTrue(sent.isEmpty())
        advanceTimeBy(2)
        runCurrent()
        assertEquals(listOf(true), timedOut)
        assertEquals("the begin may now overtake the pending write", listOf("playback_begin/b", "playback_prepare/b"), sent)
        assertEquals(1, drain.size)

        held.getValue("playback_position/a").complete(Unit)
        runCurrent()
        assertEquals(0, drain.size)
    }

    @Test
    fun `nothing to wait for begins at once`() = runTest {
        val drain = ReporterDrain()
        val finished = reporter("a").also { it.close() }
        finished.join()
        drain.add(finished)
        val timedOut = mutableListOf<Boolean>()
        start(drain, { 1 }, "b", timedOut)
        runCurrent()
        assertEquals(listOf(false), timedOut)
        assertEquals(listOf("playback_close/a", "playback_begin/b", "playback_prepare/b"), sent)
        assertFalse(drain.size > 0)
    }
}
