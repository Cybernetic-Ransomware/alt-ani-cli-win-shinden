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
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment

/** A `playback_begin` reply that lands after a close or a later start must not start or replace anything. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class StaleStartTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val name = "stale-start-${UUID.randomUUID()}"
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

    private fun listing(index: Int) = runBlocking { ControlledEnvironment.listing(runtime, index) }

    private fun resolves() = ControlledEnvironment.serverLog().getInt("resolves")

    private fun sessionsBegun(): List<String> =
        hold.replies.filter { it.first == "playback_begin" }.map { it.second.getJSONObject("data").getJSONObject("playback_session").getString("id") }

    private fun awaitClosed(sessionId: String) {
        val deadline = SystemClock.elapsedRealtime() + 10_000
        while (hold.calls.none { (method, args) -> method == "playback_close" && args.getJSONObject("playback_session").getString("id") == sessionId }) {
            assertTrue("abandoned viewing $sessionId never closed", SystemClock.elapsedRealtime() < deadline)
            Thread.sleep(100)
        }
    }

    private fun during(ms: Long, check: () -> Unit) {
        val until = SystemClock.elapsedRealtime() + ms
        while (SystemClock.elapsedRealtime() < until) {
            check()
            Thread.sleep(200)
        }
    }

    @Test
    fun aCloseBeforeTheBeginRepliesKeepsTheLateReplyFromStartingPlayback() {
        val a = listing(0)
        hold.hold("playback_begin")
        val started = onMain { controller.start(a.series, a.episode) }
        hold.awaitHeld()
        onMain { controller.close() }
        hold.release()
        runBlocking { started.join() }

        awaitClosed(sessionsBegun().single())
        during(3_000) {
            val (state, session, items) = onMain { Triple(controller.state.value, controller.playbackSession, controller.player.mediaItemCount) }
            assertEquals(PlaybackController.Phase.IDLE, state.phase)
            assertNull(session)
            assertEquals(0, items)
        }
        assertEquals("no prepare for the closed start", 0, resolves())
    }

    @Test
    fun aLaterStartWinsOverTheLateReplyOfAnEarlierOne() {
        val a = listing(0)
        val b = listing(1)
        hold.hold("playback_begin")
        val first = onMain { controller.start(a.series, a.episode) }
        hold.awaitHeld()
        val second = onMain { controller.start(b.series, b.episode) }
        hold.release()
        runBlocking {
            first.join()
            second.join()
        }

        val (abandoned, current) = sessionsBegun().also { assertEquals("A reaches the bridge before B", 2, it.size) }
        awaitClosed(abandoned)
        awaitOnMain("B plays", 20_000, { controller.player.isPlaying }) { it }
        during(3_000) {
            assertEquals(current, onMain { controller.playbackSession }!!.getString("id"))
            assertTrue(onMain { controller.player.isPlaying })
        }
        assertEquals("only B was prepared", 1, resolves())
        assertTrue(hold.calls.none { (method, args) -> method == "playback_prepare" && args.getJSONObject("playback_session").getString("id") == abandoned })
    }
}
