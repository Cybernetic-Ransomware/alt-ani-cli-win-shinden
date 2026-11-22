package pl.altani.spike.offline

import android.system.Os
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.playback.requireControlledServer

/** Cancel, delete and restore on the production store: only the app's own, identified files are ever removed. */
@RunWith(AndroidJUnit4::class)
class OfflineHygieneTest {
    @get:Rule
    val outcomes = DeviceOutcomes()

    private val runtimes = Runtimes()

    @Before
    fun setUp() = requireControlledServer()

    @After
    fun tearDown() = runtimes.closeAll()

    private fun start(runtime: BridgeRuntime): ExecutorStart = runBlocking { ControlledEnvironment.executor(runtime).start() }

    private fun watchArgs(listing: ControlledEnvironment.Listing) =
        JSONObject().put("series_id", listing.series.getString("id")).put("numbers", JSONArray().put(listing.episode.get("number")))

    private fun localFile(runtime: BridgeRuntime, listing: ControlledEnvironment.Listing): File {
        val begin = JSONObject().put("series", listing.series).put("episode", listing.episode)
        val session = runtime.ok("playback_begin", begin, mutation = true).getJSONObject("playback_session")
        val prepared = runtime.ok("playback_prepare", JSONObject(begin.toString()).put("playback_session", session))
        return File(prepared.getJSONObject("target").getString("value"))
    }

    private fun downloaded(name: String): Triple<BridgeRuntime, ControlledEnvironment.Listing, String> {
        val runtime = runtimes.open(name)
        ControlledEnvironment.configure(plainScenario())
        val listing = runBlocking { ControlledEnvironment.listing(runtime) }
        runBlocking { ControlledEnvironment.download(runtime, listing) }
        val itemId = runtime.ok("library_list").getJSONArray("items").getJSONObject(0).getString("item_id")
        return Triple(runtime, listing, itemId)
    }

    private fun delete(runtime: BridgeRuntime, itemId: String): JSONObject =
        runtime.ok("library_delete", JSONObject().put("item_ids", JSONArray().put(itemId)), mutation = true)
            .getJSONArray("results").getJSONObject(0)

    @Test
    fun cancelDuringATransferRemovesThePartialOnceTheIoEnded() {
        val name = runtimes.newName()
        val library = runtimes.library(name)
        val runtime = runtimes.open(name)
        ControlledEnvironment.configure(heldScenario())
        val jobId = runBlocking { ControlledEnvironment.enqueue(runtime, ControlledEnvironment.listing(runtime)) }
        assertTrue(start(runtime) is ExecutorStart.Started)
        awaitHeld(library)
        val revision = runBlocking { ControlledEnvironment.job(runtime, jobId) }.getInt("revision")
        runtime.ok("queue_cancel", JSONObject().put("job_id", jobId).put("expected_revision", revision), mutation = true)
        // the partial is still in use until the held read returns: nothing may be removed before that
        assertTrue(partials(library).isNotEmpty())
        releaseAfterTheMonitor()
        runBlocking { ControlledEnvironment.awaitJob(runtime, jobId, "cancelled") }
        waitUntil("the partial removed") { workEntries(library).none { "/" in it } }
        assertEquals(0, runtime.ok("library_list").getJSONArray("items").length())
    }

    @Test
    fun deletingAnItemRemovesItsFileAndKeepsTheWatchState() {
        val (runtime, listing, itemId) = downloaded(runtimes.newName())
        val file = localFile(runtime, listing)
        val session = runtime.ok("playback_begin", JSONObject().put("series", listing.series).put("episode", listing.episode), mutation = true)
            .getJSONObject("playback_session")
        runtime.ok(
            "playback_position",
            JSONObject().put("playback_session", session).put("seq", 1).put("position_ms", 42_000).put("duration_ms", 100_000),
            mutation = true,
        )
        val before = runtime.ok("watch_get", watchArgs(listing)).toString()
        assertTrue("42000" in before)

        assertEquals("ok", delete(runtime, itemId).getString("result"))

        assertFalse(file.exists())
        assertEquals(0, runtime.ok("library_list").getJSONArray("items").length())
        assertEquals(before, runtime.ok("watch_get", watchArgs(listing)).toString())
    }

    @Test
    fun aFailedDeleteStaysRetryableAndFinishesAfterARestart() {
        val name = runtimes.newName()
        val (runtime, listing, itemId) = downloaded(name)
        val file = localFile(runtime, listing)
        val parent = file.parentFile!!
        Os.chmod(parent.absolutePath, "500".toInt(8))
        val refused = try {
            delete(runtime, itemId)
        } finally {
            Os.chmod(parent.absolutePath, "700".toInt(8))
        }
        if (refused.getString("result") == "ok") DeviceOutcomes.notExecutable("a read-only directory did not refuse unlink here")
        assertEquals("failed", refused.getString("result"))
        assertEquals("storage.access_denied", refused.getString("code"))
        val item = runtime.ok("library_item", JSONObject().put("item_id", itemId)).getJSONObject("item")
        assertEquals("deleting", item.getString("status"))
        assertTrue(file.exists())
        runtimes.close(runtime)

        val again = runtimes.open(name)
        val started = start(again) as ExecutorStart.Started
        assertEquals(1, started.recovery.getJSONObject("counts").getInt("deletes_finished"))
        assertFalse(file.exists())
        assertEquals(0, again.ok("library_list").getJSONArray("items").length())
    }

    @Test
    fun aDatabaseRestoredWithoutItsLibraryMarksItemsMissingAndKeepsHistory() {
        val name = runtimes.newName()
        val (runtime, listing, _) = downloaded(name)
        val session = runtime.ok("playback_begin", JSONObject().put("series", listing.series).put("episode", listing.episode), mutation = true)
            .getJSONObject("playback_session")
        runtime.ok(
            "playback_position",
            JSONObject().put("playback_session", session).put("seq", 1).put("position_ms", 7_000).put("duration_ms", 100_000),
            mutation = true,
        )
        val watch = runtime.ok("watch_get", watchArgs(listing)).toString()
        val prefs = runtime.ok("preferences_get").toString()
        val jobs = runtime.ok("queue_snapshot").getJSONArray("jobs").length()
        runtimes.close(runtime)
        // what Auto Backup restores: the database without library/, which the backup rules leave out
        assertTrue(runtimes.library(name).deleteRecursively())

        val again = runtimes.open(name)
        assertTrue(start(again) is ExecutorStart.Started)
        val item = again.ok("library_list").getJSONArray("items").getJSONObject(0)
        assertEquals("missing", item.getString("status"))
        assertEquals(watch, again.ok("watch_get", watchArgs(listing)).toString())
        assertEquals(prefs, again.ok("preferences_get").toString())
        assertEquals(jobs, again.ok("queue_snapshot").getJSONArray("jobs").length())
    }
}
