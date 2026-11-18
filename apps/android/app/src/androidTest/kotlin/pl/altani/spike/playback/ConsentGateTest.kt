package pl.altani.spike.playback

import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.util.Collections
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
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
    private val log = Collections.synchronizedList(mutableListOf<String>())
    private val hold = CallHold()
    private var runtime: BridgeRuntime? = null
    private var controller: PlaybackController? = null
    private lateinit var listing: ControlledEnvironment.Listing

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
    private fun start(vararg extra: Pair<String, Any?>, stuckMs: Int? = STUCK_MS): PlaybackController {
        val name = "consent-${UUID.randomUUID()}".also { names += it }
        val rt = ControlledEnvironment.runtime(app, name, wrap = hold::wrap).also { runtime = it }
        ControlledEnvironment.configure(scenario("hls", "segment_delay_ms" to 1_000, *extra))
        listing = runBlocking { ControlledEnvironment.listing(rt) }
        val created = onMain { PlaybackController(app, rt, scope, { log += it }, stuckBufferingTimeoutMs = stuckMs) }
        controller = created
        onMain { created.start(listing.series, listing.episode) }
        awaitOnMain("6 s buffered", 40_000, { created.player.isPlaying to created.player.totalBufferedDuration }) { (playing, ahead) ->
            playing && ahead >= 6_000
        }
        return created
    }

    private fun call(method: String, args: JSONObject, mutation: Boolean = true): JSONObject {
        val reply = runBlocking { runtime!!.call(method, args, mutation = mutation) }
        assertTrue("$method: $reply", reply is BridgeResult.Ok)
        return (reply as BridgeResult.Ok).data
    }

    private fun network(kind: String) = call("network_set", JSONObject().put("kind", kind))

    private fun resolves() = ControlledEnvironment.serverLog().getInt("resolves")

    private fun PlaybackController.awaitHeld() = awaitOnMain("held", 5_000, { state.value.consentRequired }) { it }

    private fun PlaybackController.awaitWaitingForConsent() {
        awaitHeld()
        val waiting = awaitOnMain("buffer played out", 30_000, { state.value }) { state ->
            check(state.phase != PlaybackController.Phase.STOPPED) { "stopped: ${state.error}" }
            state.phase == PlaybackController.Phase.WAITING_CONSENT
        }
        assertEquals("a hold is not a user pause", 0, waiting.pauses)
    }

    /** No request reaches the server for longer than the shortened detector, and the player neither fails nor plays. */
    private fun PlaybackController.assertHeldQuietly() {
        val requests = mediaRequests().size
        during(STUCK_MS + 2_000L) {
            val state = onMain { state.value }
            assertNull("failed while held", state.error)
            assertEquals(PlaybackController.Phase.WAITING_CONSENT, state.phase)
        }
        assertEquals("requests while held", requests, mediaRequests().size)
        assertFalse(onMain { player.playWhenReady })
    }

    private fun during(ms: Long, check: () -> Unit) {
        val until = SystemClock.elapsedRealtime() + ms
        while (SystemClock.elapsedRealtime() < until) {
            check()
            Thread.sleep(200)
        }
    }

    private fun PlaybackController.awaitPlayingOnItsOwn() {
        val requests = mediaRequests().size
        awaitOnMain("playing again", 10_000, { player.isPlaying }) { it }
        val from = onMain { player.currentPosition }
        awaitOnMain("position moves on", 15_000, { player.currentPosition }) { it >= from + 3_000 }
        assertTrue("transfers resumed", mediaRequests().size > requests)
        assertEquals(0, onMain { state.value.pauses })
    }

    private fun PlaybackController.allow() = runBlocking { onMain { allowCellular() }.join() }

    @Test
    fun mobileDataHoldsTransfersUntilConsentThenPlaybackResumesOnItsOwn() {
        val player = start()
        network("cellular")
        player.awaitWaitingForConsent()
        player.assertHeldQuietly()
        player.allow()
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
        player.allow()
        network("cellular")
        val requests = mediaRequests().size
        val from = onMain { player.player.currentPosition }
        awaitOnMain("position moves on", 20_000, { player.player.currentPosition }) { it >= from + 6_000 }
        assertFalse(onMain { player.state.value.consentRequired })
        assertTrue("transfers went on", mediaRequests().size > requests)
    }

    /** A request has passed the open gate and the server holds its redirect for 5 s. */
    private fun startWithAHeldRedirect(): PlaybackController {
        val player = start(
            "ttl_sec" to 600,
            "redirect_files" to JSONArray().put(REDIRECTED),
            "redirect_delay_ms" to 5_000,
            // the redirect stalls the refreshed playback too, longer than the shortened detector allows
            stuckMs = null,
        )
        val deadline = SystemClock.elapsedRealtime() + 30_000
        while (mediaRequests().none { it.optString("file") == REDIRECTED && it.optInt("status") == 302 }) {
            assertTrue("redirect of $REDIRECTED not held", SystemClock.elapsedRealtime() < deadline)
            Thread.sleep(100)
        }
        return player
    }

    /** The gate closes during the held redirect, then the redirect target has expired. */
    private fun startWithARefreshNeededWhileHeld(): PlaybackController {
        val player = startWithAHeldRedirect()
        network("cellular")
        player.awaitHeld()
        ControlledEnvironment.expireUrls()
        awaitOnMain("refresh asked for", 15_000, { log.any { it.startsWith("url expired") } }) { it }
        val state = onMain { player.state.value }
        assertEquals(PlaybackController.Phase.WAITING_CONSENT, state.phase)
        assertTrue("the expired request reached the server", mediaRequests().any { it.optString("reason") == "expired" })
        return player
    }

    private fun PlaybackController.assertNoRefreshFor(ms: Long) = during(ms) {
        assertEquals("stream_refresh while held", 0, onMain { state.value.refreshes })
        assertEquals(1, resolves())
    }

    @Test
    fun aRefreshNeededWhileHeldWaitsForConsentAndThenRunsOnce() {
        val player = startWithARefreshNeededWhileHeld()
        player.assertNoRefreshFor(4_000)
        assertNull(onMain { player.state.value.error })
        player.allow()
        awaitOnMain("refreshed", 20_000, { player.state.value }) { it.refreshes == 1 && it.phase == PlaybackController.Phase.PLAYING }
        val from = onMain { player.player.currentPosition }
        awaitOnMain("position moves on", 20_000, { player.player.currentPosition }) { it >= from + 3_000 }
        during(4_000) { assertEquals(1, onMain { player.state.value.refreshes }) }
        assertEquals("one prepare and one refresh", 2, resolves())
    }

    @Test
    fun closingThePlaybackDropsTheWaitingRefresh() {
        val player = startWithARefreshNeededWhileHeld()
        onMain { player.close() }
        network("wifi")
        during(4_000) {
            assertEquals(PlaybackController.Phase.IDLE, onMain { player.state.value.phase })
            assertEquals(1, resolves())
        }
    }

    @Test
    fun anotherEpisodeDropsTheWaitingRefreshAndAsksForItsOwnConsent() {
        val player = startWithARefreshNeededWhileHeld()
        val next = runBlocking { ControlledEnvironment.listing(runtime!!, 1) }
        runBlocking { onMain { player.start(next.series, next.episode) }.join() }
        val waiting = onMain { player.state.value }
        assertEquals(PlaybackController.Phase.WAITING_CONSENT to true, waiting.phase to waiting.consentRequired)
        assertEquals("the bridge refused the prepare before any resolve", 1, resolves())
        network("wifi")
        awaitOnMain("next episode plays", 20_000, { player.player.isPlaying }) { it }
        assertEquals("only the new episode's prepare", 2, resolves())
        during(3_000) { assertEquals("the old refresh never ran", 0, onMain { player.state.value.refreshes }) }
    }

    /** The redirect target has expired on Wi-Fi and the bridge holds the `stream_refresh` call. */
    private fun startWithARefreshRunning(): PlaybackController {
        val player = startWithAHeldRedirect()
        hold.hold("stream_refresh")
        ControlledEnvironment.expireUrls()
        hold.awaitHeld()
        assertEquals(PlaybackController.Phase.REFRESHING, onMain { player.state.value.phase })
        return player
    }

    private fun PlaybackController.assertRefreshedButPaused() {
        awaitOnMain("refreshed source prepared", 20_000, { state.value.refreshes to player.playbackState }) { (refreshes, playback) ->
            refreshes == 1 && playback != Player.STATE_IDLE
        }
        during(3_000) {
            val (state, playing) = onMain { state.value to player.playWhenReady }
            assertFalse("started on its own", playing)
            assertNull(state.error)
            assertEquals(PlaybackController.Phase.PAUSED, state.phase)
        }
        assertEquals("one prepare and one refresh", 2, resolves())
    }

    @Test
    fun homeDuringARunningRefreshKeepsTheRefreshedPlaybackPaused() {
        val player = startWithARefreshRunning()
        onMain { player.onBackground() }
        hold.release()
        player.assertRefreshedButPaused()
    }

    @Test
    fun aUserPauseDuringARunningRefreshIsKept() {
        val player = startWithARefreshRunning()
        onMain { player.player.pause() }
        hold.release()
        player.assertRefreshedButPaused()
    }

    @Test
    fun homeWhileARefreshWaitsKeepsThePlaybackPausedBackOnWifi() {
        val player = startWithARefreshNeededWhileHeld()
        onMain { player.onBackground() }
        network("wifi")
        player.assertRefreshedButPaused()
    }

    @Test
    fun aUserPauseWhileARefreshWaitsIsKeptAfterConsent() {
        val player = startWithARefreshNeededWhileHeld()
        onMain { player.player.pause() }
        player.allow()
        player.assertRefreshedButPaused()
    }

    /** The user pauses while the buffer still plays behind the closed gate; [reopen] then lets transfers go on. */
    private fun aUserPauseIsKeptWhen(reopen: (PlaybackController) -> Unit) {
        val player = start()
        network("cellular")
        player.awaitHeld()
        onMain { player.player.pause() }
        reopen(player)
        awaitOnMain("gate open", 10_000, { player.gate.isOpen }) { it }
        during(3_000) {
            val (state, playing) = onMain { player.state.value to player.player.playWhenReady }
            assertFalse("started on its own", playing)
            assertEquals(PlaybackController.Phase.PAUSED to 1, state.phase to state.pauses)
        }
    }

    @Test
    fun aUserPauseWhileWaitingIsKeptAfterConsent() = aUserPauseIsKeptWhen { it.allow() }

    @Test
    fun aUserPauseWhileWaitingIsKeptBackOnWifi() = aUserPauseIsKeptWhen { network("wifi") }

    /** Stops the events pump, runs [whileDeaf] and starts it again; the events in between are skipped, never replayed. */
    private fun missEvents(whileDeaf: () -> Unit) {
        val events = runtime!!.events
        val heads = events.state.value.heads
        runBlocking { events.stop() }
        whileDeaf()
        events.start()
        awaitOnMain("resynced", 10_000, { events.state.value.heads }) { it > heads }
    }

    @Test
    fun aMissedConsentNoticeIsRecoveredByTheResync() {
        val player = start()
        missEvents {
            network("cellular")
            Thread.sleep(1_000)
            assertFalse("no event reached the controller", onMain { player.state.value.consentRequired })
        }
        player.awaitWaitingForConsent()
        player.assertHeldQuietly()
    }

    @Test
    fun theResyncTakesAConsentGivenWhileEventsWereLostFromTheBridge() {
        val player = start()
        missEvents {
            call("playback_allow_cellular", JSONObject().put("playback_session", onMain { player.playbackSession }))
            network("cellular")
        }
        val requests = mediaRequests().size
        during(4_000) { assertFalse(onMain { player.state.value.consentRequired || !player.gate.isOpen }) }
        assertTrue("transfers went on", mediaRequests().size > requests)
    }

    @Test
    fun aConsentStillHeldReopensTheGateWhenMobileDataComesBackAfterOffline() {
        val player = start()
        player.allow()
        network("offline")
        player.awaitHeld()
        network("cellular")
        player.awaitPlayingOnItsOwn()
        assertFalse(onMain { player.state.value.consentRequired })
    }

    @Test
    fun theResyncReopensTheGateForAConsentStillHeldAfterOffline() {
        val player = start()
        player.allow()
        missEvents { network("offline") }
        player.awaitHeld()
        missEvents { network("cellular") }
        player.awaitPlayingOnItsOwn()
        assertFalse(onMain { player.state.value.consentRequired })
    }

    /** The main thread, where the controller takes its events, is blocked while the bridge publishes more than the pump buffers. */
    @Test
    fun aNetworkChangeFollowedByMoreEventsThanTheBufferStillClosesTheGate() {
        val player = start()
        val events = runtime!!.events
        val blocked = CountDownLatch(1)
        val free = CountDownLatch(1)
        Handler(Looper.getMainLooper()).post {
            blocked.countDown()
            free.await(60, TimeUnit.SECONDS)
        }
        assertTrue(blocked.await(10, TimeUnit.SECONDS))
        try {
            val received = events.state.value.received
            network("cellular")
            // executor.state, which the controller ignores; the bridge journal keeps far more, so it reports no resync
            repeat(FILLER) { call("executor_stop", JSONObject()) }
            val deadline = SystemClock.elapsedRealtime() + 3_000
            while (events.state.value.received < received + FILLER + 1 && SystemClock.elapsedRealtime() < deadline) {
                Thread.sleep(100)
            }
        } finally {
            free.countDown()
        }
        player.awaitWaitingForConsent()
        player.assertHeldQuietly()
    }

    @Test
    fun theResyncClosesTheGateWhenTheBridgeEndedTheConsent() {
        val player = start()
        player.allow()
        missEvents {
            // another viewing ends this one's consent (P3); the controller only learns it from the bridge
            call("playback_begin", JSONObject().put("series", listing.series).put("episode", listing.episode))
            network("cellular")
        }
        player.awaitHeld()
    }

    private companion object {
        const val STUCK_MS = 3_000
        const val REDIRECTED = "hls/seg_006.ts"
        const val FILLER = 300
    }
}
