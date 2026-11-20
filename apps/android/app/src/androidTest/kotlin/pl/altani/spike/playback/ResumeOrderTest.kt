package pl.altani.spike.playback

import android.os.SystemClock
import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.util.UUID
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
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment

/** A new start of the same episode lets the old viewing's last position land before its `playback_begin`, and start over ignores it. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class ResumeOrderTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val name = "resume-order-${UUID.randomUUID()}"
    private val hold = CallHold()
    private lateinit var runtime: BridgeRuntime
    private lateinit var controller: PlaybackController

    @Before
    fun setUp() {
        requireControlledServer()
        runtime = ControlledEnvironment.runtime(app, name, wrap = hold::wrap)
        ControlledEnvironment.configure(scenario("hls"))
        controller = onMain { PlaybackController(app, runtime, scope, {}) }
    }

    @After
    fun tearDown() {
        if (::controller.isInitialized) onMain { controller.release() }
        scope.cancel()
        if (::runtime.isInitialized) assertTrue("runtime did not end", runBlocking { runtime.shutdown(10_000) }.ended)
        File(app.filesDir, "controlled-content/$name").deleteRecursively()
    }

    private fun listing() = runBlocking { ControlledEnvironment.listing(runtime, 0) }

    private fun playTo(atLeastMs: Long): Long =
        awaitOnMain("position >= $atLeastMs", 30_000, { controller.player.currentPosition to controller.player.isPlaying }) { (position, playing) ->
            playing && position >= atLeastMs
        }.first

    private fun viewing(): String = onMain { controller.playbackSession }!!.getString("id")

    private fun index(kind: String, method: String, viewing: String? = null, from: Int = 0): Int {
        val found = hold.timeline.withIndex().drop(from).firstOrNull { (_, entry) ->
            val (k, m, payload) = entry
            k == kind && m == method && (viewing == null || payload.optJSONObject("playback_session")?.optString("id") == viewing)
        }
        assertTrue("$kind $method${viewing?.let { " of $it" } ?: ""} not seen", found != null)
        return found!!.index
    }

    /** Calls only: a reply carries no viewing, it is the next reply of that method after its call. */
    private fun lastIndex(kind: String, method: String, viewing: String): Int =
        hold.timeline.indexOfLast { (k, m, payload) -> k == kind && m == method && payload.optJSONObject("playback_session")?.optString("id") == viewing }

    private fun preparedResume(from: Int): Long {
        val at = index("reply", "playback_prepare", from = from)
        return hold.timeline[at].third.getJSONObject("data").getJSONObject("resume").optLong("position_ms")
    }

    @Test
    fun aSourceChangeOfTheSameEpisodeKeepsTheLastPositionBeforeTheNewBegin() {
        val a = listing()
        onMain { controller.start(a.series, a.episode) }
        playTo(4_000)
        val old = viewing()
        val mark = hold.timeline.size
        val at = onMain {
            // read first: the stop clears the player
            controller.player.currentPosition.also { controller.start(a.series, a.episode) }
        }
        playTo(at)

        val lastWrite = lastIndex("call", "playback_position", old)
        val written = index("reply", "playback_position", from = lastWrite)
        val closed = index("reply", "playback_close", from = index("call", "playback_close", old, from = mark))
        val begin = index("call", "playback_begin", from = mark)
        val prepare = index("call", "playback_prepare", from = begin)
        assertTrue("old position answered before the begin: $written < $begin", written < begin)
        assertTrue("old viewing closed before the begin: $closed < $begin", closed < begin)
        assertTrue(begin < prepare)
        assertTrue("the old write was kept", hold.timeline[written].third.getJSONObject("data").getBoolean("accepted"))
        val resumed = preparedResume(prepare)
        assertTrue("resume $resumed is the position at the change ($at)", resumed >= at - 1_000)
    }

    @Test
    fun startOverBeginsAtZeroWhileTheSavedPositionStaysForResume() {
        val a = listing()
        onMain { controller.start(a.series, a.episode) }
        val saved = playTo(6_000)
        onMain { controller.close() }
        val mark = hold.timeline.size

        onMain { controller.start(a.series, a.episode, fromStart = true) }
        awaitOnMain("start over plays", 30_000, { controller.player.isPlaying }) { it }
        val position = onMain { controller.player.currentPosition }
        assertTrue("started at $position", position < 3_000)
        assertTrue("prepare still reported the saved position", preparedResume(mark) >= saved - 1_000)
    }

    @Test
    fun anUnansweredPositionWriteDelaysTheNextBeginByTheDrainLimitOnly() {
        val a = listing()
        onMain { controller.start(a.series, a.episode) }
        playTo(3_000)
        val old = viewing()
        hold.hold("playback_position")
        val mark = hold.timeline.size
        val started = SystemClock.elapsedRealtime()
        onMain { controller.start(a.series, a.episode) }
        hold.awaitHeld()

        Thread.sleep(ReporterDrain.DRAIN_MS - 500)
        assertFalse("no begin while the drain waits", hold.timeline.drop(mark).any { (k, m, _) -> k == "call" && m == "playback_begin" })
        val begin = awaitOnMain("begin after the limit", 10_000, { hold.timeline.drop(mark).any { (k, m, _) -> k == "call" && m == "playback_begin" } }) { it }
        assertTrue(begin)
        assertTrue("the limit, not the bridge, released it", SystemClock.elapsedRealtime() - started < ReporterDrain.DRAIN_MS + 5_000)

        hold.release()
        awaitOnMain("new viewing plays", 30_000, { controller.player.isPlaying }) { it }
        // after the timeout the overtaken write is refused by the engine: the loss the drain limit allows
        val call = lastIndex("call", "playback_position", old)
        val reply = awaitOnMain("held write answered", 10_000, {
            hold.timeline.withIndex().drop(call).firstOrNull { (_, e) -> e.first == "reply" && e.second == "playback_position" }?.index ?: -1
        }) { it >= 0 }
        assertEquals(false, hold.timeline[reply].third.getJSONObject("data").getBoolean("accepted"))
    }
}
