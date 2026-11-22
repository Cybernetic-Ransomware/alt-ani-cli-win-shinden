package pl.altani.spike.offline

import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.playback.PlaybackController
import pl.altani.spike.playback.awaitOnMain
import pl.altani.spike.playback.mediaRequests
import pl.altani.spike.playback.onMain
import pl.altani.spike.playback.requireControlledServer

/** E1, H1 and R2: queue → executor → production store → library → local playback, then the network is gone. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class OfflineEndToEndTest {
    @get:Rule
    val outcomes = DeviceOutcomes()

    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val runtimes = Runtimes()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var controller: PlaybackController? = null

    @Before
    fun setUp() = requireControlledServer()

    @After
    fun tearDown() {
        controller?.let { onMain { it.release() } }
        scope.cancel()
        runtimes.closeAll()
    }

    private fun prepare(runtime: BridgeRuntime, listing: ControlledEnvironment.Listing): JSONObject {
        val begin = JSONObject().put("series", listing.series).put("episode", listing.episode)
        val session = runtime.ok("playback_begin", begin, mutation = true).getJSONObject("playback_session")
        return runtime.ok("playback_prepare", JSONObject(begin.toString()).put("playback_session", session))
    }

    private fun play(runtime: BridgeRuntime, listing: ControlledEnvironment.Listing): String? {
        controller?.let { onMain { it.release() } }
        val created = onMain { PlaybackController(app, runtime, scope, { }) }
        controller = created
        onMain { created.start(listing.series, listing.episode, null) }
        awaitOnMain("local playback past 3 s", 45_000, { created.player.currentPosition to created.state.value }) { (position, state) ->
            check(state.phase != PlaybackController.Phase.STOPPED) { "stopped: ${state.error}" }
            position >= 3_000
        }
        return onMain { created.state.value.mode }
    }

    @Test
    fun downloadVerifyAndPlayOffline() {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        val library = runtimes.library(name)
        ControlledEnvironment.configure(plainScenario())
        val listing = runBlocking { ControlledEnvironment.listing(runtime) }

        runBlocking { ControlledEnvironment.download(runtime, listing) }

        val items = runtime.ok("library_list").getJSONArray("items").objects()
        assertEquals(1, items.size)
        assertEquals(VIDEO_BYTES, items[0].getLong("size_bytes"))
        val verified = runtime.ok("library_verify", JSONObject().put("deep", true), mutation = true).getJSONArray("results")
        assertEquals("ok", verified.getJSONObject(0).getString("result"))

        val local = prepare(runtime, listing)
        assertEquals("local", local.getString("mode"))
        val target = local.getJSONObject("target")
        assertEquals("file", target.getString("kind"))
        val file = File(target.getString("value"))
        assertTrue("$file outside $library", file.canonicalPath.startsWith(library.canonicalPath + File.separator))
        assertFalse(".work" in file.path.split(File.separator))
        assertEquals(VIDEO_BYTES, file.length())
        waitUntil("the partial removed") { partials(library).isEmpty() && workEntries(library).none { "/" in it } }

        assertEquals("file", play(runtime, listing))

        ControlledEnvironment.configure(plainScenario())
        runtime.ok("network_set", JSONObject().put("kind", "offline"), mutation = true)
        ControlledEnvironment.expireUrls()
        assertEquals("local", prepare(runtime, listing).getString("mode"))
        assertEquals("file", play(runtime, listing))
        assertEquals("no request while the network is gone", emptyList<JSONObject>(), mediaRequests())
    }

    @Test
    fun aRestartKeepsTheItemAndTheNextStartTidiesTheEmptyJobDirectory() {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        val library = runtimes.library(name)
        ControlledEnvironment.configure(plainScenario())
        val listing = runBlocking { ControlledEnvironment.listing(runtime) }
        val jobId = runBlocking { ControlledEnvironment.download(runtime, listing) }
        // the partial goes with the completion; its now empty job directory waits for the next check_root
        waitUntil("only the empty job directory left") { workEntries(library) == listOf(jobId) }
        runtimes.close(runtime)

        val again = runtimes.open(name)
        val started = runBlocking { ControlledEnvironment.executor(again).start() }
        assertTrue("$started", started is ExecutorStart.Started)
        assertEquals(0, (started as ExecutorStart.Started).recovery.getInt("orphans"))
        assertEquals(emptyList<String>(), workEntries(library))
        assertEquals(1, again.ok("library_list").getJSONArray("items").length())
        assertEquals("local", prepare(again, listing).getString("mode"))
    }
}
