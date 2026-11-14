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
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment

/** The chosen T9 hold through the bridge: `network_set(cellular)` holds player transfers until consent or Wi-Fi. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class ConsentGateTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val names = mutableListOf<String>()
    private var runtime: BridgeRuntime? = null
    private var controller: PlaybackController? = null

    @Before
    fun setUp() = requireControlledServer()

    @After
    fun tearDown() {
        controller?.let { onMain { it.release() } }
        scope.cancel()
        runtime?.let { assertTrue("runtime did not end", runBlocking { it.shutdown(10_000) }.ended) }
        names.forEach { File(app.filesDir, "controlled-content/$it").deleteRecursively() }
    }

    /** The no-progress detector is cut to 3 s, so a wait without the controller's pause would fail the player here. */
    private fun start(): PlaybackController {
        val name = "consent-${UUID.randomUUID()}".also { names += it }
        val rt = ControlledEnvironment.runtime(app, name).also { runtime = it }
        ControlledEnvironment.configure(scenario("hls", "segment_delay_ms" to 1_000))
        val listing = runBlocking { ControlledEnvironment.listing(rt) }
        val created = onMain { PlaybackController(app, rt, scope, {}, stuckBufferingTimeoutMs = STUCK_MS) }
        controller = created
        onMain { created.start(listing.series, listing.episode) }
        awaitOnMain("6 s buffered", 40_000, { created.player.isPlaying to created.player.totalBufferedDuration }) { (playing, ahead) ->
            playing && ahead >= 6_000
        }
        return created
    }

    private fun network(kind: String) {
        val reply = runBlocking { runtime!!.call("network_set", JSONObject().put("kind", kind), mutation = true) }
        assertTrue("network_set $kind: $reply", reply is BridgeResult.Ok)
    }

    private fun PlaybackController.awaitWaitingForConsent() {
        awaitOnMain("held", 5_000, { state.value.consentRequired }) { it }
        val waiting = awaitOnMain("buffer played out", 30_000, { state.value }) { state ->
            check(state.phase != PlaybackController.Phase.STOPPED) { "stopped: ${state.error}" }
            state.phase == PlaybackController.Phase.WAITING_CONSENT
        }
        assertEquals("a hold is not a user pause", 0, waiting.pauses)
    }

    /** No request reaches the server for longer than the shortened detector, and the player neither fails nor plays. */
    private fun PlaybackController.assertHeldQuietly() {
        val requests = mediaRequests().size
        val until = SystemClock.elapsedRealtime() + STUCK_MS + 2_000
        while (SystemClock.elapsedRealtime() < until) {
            val state = onMain { state.value }
            assertNull("failed while held", state.error)
            assertEquals(PlaybackController.Phase.WAITING_CONSENT, state.phase)
            Thread.sleep(200)
        }
        assertEquals("requests while held", requests, mediaRequests().size)
        assertFalse(onMain { player.playWhenReady })
    }

    private fun PlaybackController.awaitPlayingOnItsOwn() {
        val requests = mediaRequests().size
        awaitOnMain("playing again", 10_000, { player.isPlaying }) { it }
        val from = onMain { player.currentPosition }
        awaitOnMain("position moves on", 15_000, { player.currentPosition }) { it >= from + 3_000 }
        assertTrue("transfers resumed", mediaRequests().size > requests)
        assertEquals(0, onMain { state.value.pauses })
    }

    @Test
    fun mobileDataHoldsTransfersUntilConsentThenPlaybackResumesOnItsOwn() {
        val player = start()
        network("cellular")
        player.awaitWaitingForConsent()
        player.assertHeldQuietly()
        runBlocking { onMain { player.allowCellular() }.join() }
        assertFalse(onMain { player.state.value.consentRequired })
        player.awaitPlayingOnItsOwn()
    }

    @Test
    fun backOnWifiResumesWithoutConsent() {
        val player = start()
        network("cellular")
        player.awaitWaitingForConsent()
        network("wifi")
        player.awaitPlayingOnItsOwn()
    }

    @Test
    fun consentGivenBeforeTheSwitchKeepsTransfersGoing() {
        val player = start()
        runBlocking { onMain { player.allowCellular() }.join() }
        network("cellular")
        val requests = mediaRequests().size
        val from = onMain { player.player.currentPosition }
        awaitOnMain("position moves on", 20_000, { player.player.currentPosition }) { it >= from + 6_000 }
        assertFalse(onMain { player.state.value.consentRequired })
        assertTrue("transfers went on", mediaRequests().size > requests)
    }

    private companion object {
        const val STUCK_MS = 3_000
    }
}
