package pl.altani.spike

import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chaquo.python.Python
import java.io.File
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.bridge.ChaquopyEndpoint
import pl.altani.spike.bridge.isCancelled

/** The real Python bridge under the Kotlin client; each test has its own database next to the app's. */
@RunWith(AndroidJUnit4::class)
class BridgeInstrumentedTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val dbFiles = mutableListOf<File>()
    private val runtimes = mutableListOf<BridgeRuntime>()

    private fun newBridge() = ChaquopyEndpoint(Python.getInstance().getModule("android_host").callAttr("create_bridge"))

    private fun runtime(db: File = File(app.filesDir, "it-${UUID.randomUUID()}.db").also { dbFiles += it }): BridgeRuntime =
        BridgeRuntime(
            endpointFactory = ::newBridge,
            engineArgs = {
                JSONObject().put("cache_dir", app.cacheDir.absolutePath).put("history_path", db.absolutePath).put("network", "wifi")
            },
        ).also { runtimes += it }

    /** Until the bridge itself has registered [count] operations; `engine_status` does not count itself. */
    private suspend fun BridgeRuntime.awaitRunningInPython(count: Int) {
        while ((call("engine_status") as BridgeResult.Ok).data.getInt("running_operations") < count) {
            yield()
        }
    }

    private fun test(body: suspend CoroutineScope.() -> Unit) = runBlocking { withTimeout(60_000) { body() } }

    @After
    fun tearDown() = runBlocking {
        runtimes.forEach { it.shutdown(5_000) }
        dbFiles.forEach { db -> db.parentFile?.listFiles { f -> f.name.startsWith(db.name) }?.forEach { it.delete() } }
    }

    @Test
    fun bridgeInfoReportsContract21() = test {
        val reply = runtime().call("bridge_info") as BridgeResult.Ok
        assertEquals("2.1", reply.data.getString("contract"))
        assertTrue(reply.data.getJSONObject("capabilities").getBoolean("cancel_before_start"))
    }

    @Test
    fun cancelReachesAWaitingPollWithinASecond() = test {
        val runtime = runtime()
        val head = (runtime.call("events_head") as BridgeResult.Ok).data
        val poll = launch(Dispatchers.Default) {
            runtime.call("events_next", JSONObject().put("cursor", head).put("wait_ms", 30_000))
        }
        runtime.awaitRunningInPython(1)
        val late = async(start = CoroutineStart.UNDISPATCHED) { runtime.tracker.lateResults.first() }
        val started = System.nanoTime()
        poll.cancel()
        assertTrue(runtime.tracker.awaitIdle(5_000))
        val elapsedMs = (System.nanoTime() - started) / 1_000_000

        assertTrue("took $elapsedMs ms", elapsedMs < 1_000)
        assertTrue(late.await().result.isCancelled)
    }

    @Test
    fun aCancelBeforeTheCallEndsItCancelled() = test {
        val bridge = newBridge()
        val opId = UUID.randomUUID().toString()
        assertFalse("not running yet", bridge.cancel(opId))
        val reply = BridgeResult.parse(bridge.call("bridge_info", JSONObject().put("op_id", opId).toString()))

        assertTrue(reply.isCancelled)
        assertTrue(BridgeResult.parse(bridge.shutdown(5.0)) is BridgeResult.Ok)
    }

    @Test
    fun aPositionSurvivesAFreshRuntimeOnTheSameDatabase() = test {
        val db = File(app.filesDir, "it-${UUID.randomUUID()}.db").also { dbFiles += it }
        val series = JSONObject().put("id", "it-series").put("slug", "s").put("title", "t").put("url", "https://shinden.pl/series/1-s")
        val episode = JSONObject().put("number", 1).put("title", "e").put("url", "https://shinden.pl/episode/1-s/view/1")

        val writer = runtime(db)
        val begun = (writer.call("playback_begin", JSONObject().put("series", series).put("episode", episode), mutation = true) as BridgeResult.Ok).data
        val written = writer.call(
            "playback_position",
            JSONObject().put("playback_session", begun.getJSONObject("playback_session")).put("seq", 1).put("position_ms", 61_000),
            mutation = true,
        ) as BridgeResult.Ok
        assertTrue(written.data.getBoolean("accepted"))
        assertTrue(writer.shutdown(10_000).ended)

        val reader = runtime(db)
        val watch = (reader.call("watch_get", JSONObject().put("series_id", "it-series")) as BridgeResult.Ok)
            .data.getJSONArray("episodes").getJSONObject(0)
        assertEquals(61_000, watch.getInt("position_ms"))
        assertTrue(watch.getInt("revision") >= 1)
    }

    @Test
    fun shutdownEndsAWaitingPollAndReleasesTheRuntime() = test {
        val runtime = runtime()
        val head = (runtime.call("events_head") as BridgeResult.Ok).data
        val poll = async(Dispatchers.Default) {
            runtime.call("events_next", JSONObject().put("cursor", head).put("wait_ms", 30_000))
        }
        runtime.awaitRunningInPython(1)

        val report = runtime.shutdown(10_000)
        val pollResult = poll.await() as BridgeResult.Failure

        assertTrue(report.toString(), report.ended)
        assertTrue(runtime.released)
        assertTrue(pollResult.code, pollResult.code in setOf("cancelled", "engine_closed"))
    }

    @Test
    fun recreatingTheActivityKeepsOneEngine() = test {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            repeat(10) { scenario.recreate() }
        }
        // waits for the engine the activity asked for, without creating another
        assertTrue(app.bridge.call("engine_status") is BridgeResult.Ok)
        assertEquals(1, app.bridge.engineCreations)
    }
}
