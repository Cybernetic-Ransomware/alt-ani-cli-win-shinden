package pl.altani.spike.offline

import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.playback.mediaRequests
import pl.altani.spike.playback.requireControlledServer

/** Interrupted transfers, restarts, ownership and the API gate on the production store; I/O is held by the server. */
@RunWith(AndroidJUnit4::class)
class OfflineRecoveryTest {
    @get:Rule
    val outcomes = DeviceOutcomes()

    private val runtimes = Runtimes()

    @Before
    fun setUp() = requireControlledServer()

    @After
    fun tearDown() = runtimes.closeAll()

    private fun start(runtime: BridgeRuntime): ExecutorStart = runBlocking { ControlledEnvironment.executor(runtime).start() }

    private fun listing(runtime: BridgeRuntime) = runBlocking { ControlledEnvironment.listing(runtime) }

    private fun job(runtime: BridgeRuntime, jobId: String) = runBlocking { ControlledEnvironment.job(runtime, jobId) }

    private fun await(runtime: BridgeRuntime, jobId: String, vararg states: String) =
        runBlocking { ControlledEnvironment.awaitJob(runtime, jobId, *states) }

    private fun revision(runtime: BridgeRuntime, jobId: String) =
        JSONObject().put("job_id", jobId).put("expected_revision", job(runtime, jobId).getInt("revision"))

    /** Starts a held transfer, stops the executor while its I/O is held (`ended: false`), then lets the I/O end. */
    private fun interrupt(runtime: BridgeRuntime, library: File): String {
        ControlledEnvironment.configure(heldScenario())
        val jobId = runBlocking { ControlledEnvironment.enqueue(runtime, listing(runtime)) }
        val control = ControlledEnvironment.executor(runtime)
        assertTrue(runBlocking { control.start() } is ExecutorStart.Started)
        awaitHeld(library)
        val stopping = runBlocking { control.stop(timeoutMs = 200) }
        assertEquals(ExecutorStop.StillStopping(listOf(jobId)), stopping)
        assertEquals(ExecutorStart.Closing, runBlocking { control.start() })
        ControlledEnvironment.releaseStall()
        assertEquals(ExecutorStop.Ended, runBlocking { control.stop() })
        return jobId
    }

    @Test
    fun aStopWithHeldIoEndsFalseAndKeepsOwnershipUntilTheIoEnds() {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        interrupt(runtime, runtimes.library(name))
        assertTrue(start(runtime) is ExecutorStart.Started)
    }

    @Test
    fun anInterruptedTransferResumesFromItsCheckpointWithAMatchingValidator() {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        val jobId = interrupt(runtime, runtimes.library(name))
        val done = job(runtime, jobId).getLong("bytes_done")
        assertTrue("bytes_done $done", done in 1 until PADDED_BYTES)
        runtimes.close(runtime)

        ControlledEnvironment.configure(plainScenario("pad_bytes" to PADDED_BYTES - VIDEO_BYTES))
        val again = runtimes.open(name)
        assertTrue(start(again) is ExecutorStart.Started)
        await(again, jobId, "completed")
        val calls = mediaRequests().filter { it.getString("file") == "video.mp4" }
        assertTrue("no HEAD revalidation: $calls", calls.any { it.getString("method") == "HEAD" })
        val ranged = calls.filter { it.getString("method") == "GET" && !it.isNull("range") }.map { it.getString("range") }
        assertTrue("no resumed range: $calls", ranged.isNotEmpty() && "bytes=0-" !in ranged)
        val verified = again.ok("library_verify", JSONObject().put("deep", true), mutation = true).getJSONArray("results")
        assertEquals("ok", verified.getJSONObject(0).getString("result"))
    }

    @Test
    fun aChangedResourceIsNeverAppendedToTheOldPartial() {
        val name = runtimes.newName()
        val library = runtimes.library(name)
        val runtime = runtimes.open(name)
        val jobId = interrupt(runtime, library)
        val partial = partials(library).single()
        val kept = partial.readBytes()
        runtimes.close(runtime)

        ControlledEnvironment.configure(plainScenario("media_revision" to 1, "pad_bytes" to PADDED_BYTES - VIDEO_BYTES))
        val again = runtimes.open(name)
        assertTrue(start(again) is ExecutorStart.Started)
        val waiting = await(again, jobId, "waiting")
        assertEquals("source_decision", waiting.getString("wait_reason"))
        assertEquals(0, again.ok("library_list").getJSONArray("items").length())
        assertArrayEquals(kept, partial.readBytes().copyOf(kept.size))
        assertTrue(mediaRequests().none { it.getString("method") == "GET" && !it.isNull("range") })
    }

    @Test
    fun aPausedJobKeepsItsCheckpointAcrossARestart() {
        val name = runtimes.newName()
        val library = runtimes.library(name)
        val runtime = runtimes.open(name)
        ControlledEnvironment.configure(heldScenario())
        val jobId = runBlocking { ControlledEnvironment.enqueue(runtime, listing(runtime)) }
        assertTrue(start(runtime) is ExecutorStart.Started)
        awaitHeld(library)
        runtime.ok("queue_pause", revision(runtime, jobId), mutation = true)
        releaseAfterTheMonitor()
        val paused = await(runtime, jobId, "paused")
        assertTrue(partials(library).single().length() >= paused.getLong("bytes_done"))
        assertTrue(paused.getLong("bytes_done") > 0)
        runtimes.close(runtime)

        ControlledEnvironment.configure(plainScenario("pad_bytes" to PADDED_BYTES - VIDEO_BYTES))
        val again = runtimes.open(name)
        assertTrue(start(again) is ExecutorStart.Started)
        assertEquals("paused", job(again, jobId).getString("state"))
        again.ok("queue_resume", revision(again, jobId), mutation = true)
        await(again, jobId, "completed")
        assertTrue(mediaRequests().any { it.getString("method") == "GET" && !it.isNull("range") && it.getString("range") != "bytes=0-" })
    }

    @Test
    fun losingTheNetworkHoldsTheJobAndItsReturnCompletesIt() {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        ControlledEnvironment.configure(heldScenario())
        val jobId = runBlocking { ControlledEnvironment.enqueue(runtime, listing(runtime)) }
        assertTrue(start(runtime) is ExecutorStart.Started)
        awaitHeld(runtimes.library(name))
        runtime.ok("network_set", JSONObject().put("kind", "offline"), mutation = true)
        releaseAfterTheMonitor()
        val waiting = await(runtime, jobId, "waiting")
        assertEquals("network", waiting.getString("wait_reason"))
        assertEquals(0, waiting.getInt("retry_count"))
        runtime.ok("network_set", JSONObject().put("kind", "wifi"), mutation = true)
        await(runtime, jobId, "completed")
    }

    @Test
    fun switchingToWifiOnlyStopsACellularTransfer() {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        val revision = runtime.ok("preferences_get").getInt("revision")
        val any = JSONObject().put("changes", JSONObject().put("download_network", "any")).put("expected_revision", revision)
        val after = runtime.ok("preferences_update", any, mutation = true).getInt("revision")
        runtime.ok("network_set", JSONObject().put("kind", "cellular"), mutation = true)
        ControlledEnvironment.configure(heldScenario())
        val jobId = runBlocking { ControlledEnvironment.enqueue(runtime, listing(runtime)) }
        assertTrue(start(runtime) is ExecutorStart.Started)
        awaitHeld(runtimes.library(name))
        val wifiOnly = JSONObject().put("changes", JSONObject().put("download_network", "wifi_only")).put("expected_revision", after)
        runtime.ok("preferences_update", wifiOnly, mutation = true)
        releaseAfterTheMonitor()
        assertEquals("network", await(runtime, jobId, "waiting").getString("wait_reason"))
    }

    @Test
    fun belowApi30TheExecutorRefusesWithAReadableCodeAndKeepsNoOwnership() {
        val runtime = runtimes.open(runtimes.newName(), apiLevel = 29)
        repeat(2) { assertEquals(ExecutorStart.Unsupported("storage.no_atomic_publish"), start(runtime)) }
        assertEquals("stopped", runtime.ok("executor_status").getString("state"))
    }

    @Test
    fun aSecondBridgeOnTheSameDatabaseIsBusy() {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        assertTrue(start(runtime) is ExecutorStart.Started)
        assertEquals(ExecutorStart.AlreadyRunning, start(runtime))
        // a second BridgeRuntime is a second Python Bridge: only the database's flock can refuse it
        val other = runtimes.open(name)
        assertEquals(ExecutorStart.Busy, start(other))
    }

    @Test
    fun aForeignFileTakesNoNameAndIsReportedOnEveryStartButNeverDeleted() {
        val name = runtimes.newName()
        val library = runtimes.library(name)
        val runtime = runtimes.open(name)
        ControlledEnvironment.configure(plainScenario())
        val listing = listing(runtime)
        val title = listing.series.getString("title")
        val foreign = File(library, "$title [${listing.series.getString("id")}]/$title - ep1.mp4")
        foreign.parentFile!!.mkdirs()
        foreign.writeBytes("theirs".toByteArray())
        val stray = File(library, "notes/keep.txt").apply { parentFile!!.mkdirs(); writeBytes("mine".toByteArray()) }
        runBlocking { ControlledEnvironment.download(runtime, listing) }
        assertEquals("theirs", foreign.readText())
        runtimes.close(runtime)
        repeat(3) {
            val again = runtimes.open(name)
            val started = start(again) as ExecutorStart.Started
            assertEquals(2, started.recovery.getInt("orphans"))
            assertEquals(1, again.ok("library_list").getJSONArray("items").length())
            runtimes.close(again)
        }
        assertEquals("theirs", foreign.readText())
        assertEquals("mine", stray.readText())
    }

    @Test
    fun recoveryAndTidyUpAreIdempotent() {
        val name = runtimes.newName()
        val work = File(runtimes.library(name), ".work").apply { mkdirs() }
        File(work, ".probe-${"a".repeat(32)}").writeBytes(ByteArray(0))
        File(work, "job-left-empty").mkdirs()
        File(work, "notes.txt").writeText("mine")
        val seen = (1..3).map {
            val runtime = runtimes.open(name)
            assertTrue(start(runtime) is ExecutorStart.Started)
            runtimes.close(runtime)
            workEntries(runtimes.library(name))
        }
        assertEquals(listOf("notes.txt"), seen.first())
        assertTrue(seen.all { it == seen.first() })
    }

    @Test
    fun aDatabaseCreatedWithoutAFileStoreOpensWithOne() {
        val name = runtimes.newName()
        val legacy = runtimes.open(name, storage = null)
        ControlledEnvironment.configure(plainScenario())
        val listing = listing(legacy)
        val begin = JSONObject().put("series", listing.series).put("episode", listing.episode)
        val session = legacy.ok("playback_begin", begin, mutation = true).getJSONObject("playback_session")
        legacy.ok(
            "playback_position",
            JSONObject().put("playback_session", session).put("seq", 1).put("position_ms", 5_000).put("duration_ms", 100_000),
            mutation = true,
        )
        val watchArgs = JSONObject().put("series_id", listing.series.getString("id")).put("numbers", org.json.JSONArray().put(listing.episode.get("number")))
        val watch = legacy.ok("watch_get", watchArgs).toString()
        assertEquals(ExecutorStart.Unsupported("storage.not_configured"), start(legacy))
        runtimes.close(legacy)

        val current = runtimes.open(name)
        val started = start(current) as ExecutorStart.Started
        assertEquals(0, started.recovery.getInt("orphans"))
        val counts = started.recovery.getJSONObject("counts")
        assertTrue("$counts", counts.keys().asSequence().all { counts.getInt(it) == 0 })
        assertEquals(watch, current.ok("watch_get", watchArgs).toString())
    }
}
