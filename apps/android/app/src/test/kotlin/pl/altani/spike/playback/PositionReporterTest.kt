package pl.altani.spike.playback

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.BridgeResult

@OptIn(ExperimentalCoroutinesApi::class)
class PositionReporterTest {
    private val session = JSONObject().put("id", "s1").put("series_id", "7").put("number", 1)
    private val sent = mutableListOf<Pair<String, JSONObject>>()

    private fun accepted() = BridgeResult.Ok(JSONObject().put("accepted", true))

    @Test
    fun `writes reach the bridge in seq order even when an earlier call is slow`() = runTest {
        val firstHeld = CompletableDeferred<Unit>()
        val reporter = PositionReporter(backgroundScope, session, { method, args ->
            if (sent.isEmpty()) {
                sent += method to args
                firstHeld.await()
            } else {
                sent += method to args
            }
            accepted()
        })
        reporter.explicit(1_000, 30_000)
        reporter.periodic(2_000, 30_000)
        reporter.ended(30_000, 30_000)
        reporter.close()
        firstHeld.complete(Unit)
        reporter.join()
        assertEquals(listOf("playback_position", "playback_position", "playback_ended", "playback_close"), sent.map { it.first })
        assertEquals(listOf(1L, 2L, 3L), sent.take(3).map { it.second.getLong("seq") })
        assertFalse(sent.last().second.has("seq"))
    }

    @Test
    fun `periodic writes below the rotation floor are skipped until playback passes it`() = runTest {
        val reporter = PositionReporter(backgroundScope, session, { method, args ->
            sent += method to args
            accepted()
        })
        reporter.rotationFloorMs = 10_000
        assertFalse(reporter.periodic(8_600, 30_000))
        assertFalse(reporter.periodic(9_900, 30_000))
        assertTrue(reporter.periodic(10_100, 30_000))
        assertTrue(reporter.periodic(9_000, 30_000))
        reporter.close()
        reporter.join()
        assertEquals(listOf(10_100L, 9_000L), sent.filter { it.first == "playback_position" }.map { it.second.getLong("position_ms") })
    }

    @Test
    fun `an explicit write is never held back by the floor`() = runTest {
        val reporter = PositionReporter(backgroundScope, session, { method, args ->
            sent += method to args
            accepted()
        })
        reporter.rotationFloorMs = 10_000
        assertTrue(reporter.explicit(8_500, 30_000))
        reporter.close()
        reporter.join()
        assertEquals(8_500L, sent.first().second.getLong("position_ms"))
    }

    @Test
    fun `nothing is queued after close and a rejected write is not retried`() = runTest {
        val results = mutableListOf<BridgeResult>()
        val reporter = PositionReporter(backgroundScope, session, { method, args ->
            sent += method to args
            BridgeResult.Ok(JSONObject().put("accepted", false))
        }) { _, result -> results += result }
        reporter.explicit(1_000, null)
        reporter.close()
        assertFalse(reporter.explicit(2_000, null))
        reporter.join()
        assertEquals(listOf("playback_position", "playback_close"), sent.map { it.first })
        assertFalse(sent.first().second.has("duration_ms"))
        assertEquals(2, results.size)
    }
}
