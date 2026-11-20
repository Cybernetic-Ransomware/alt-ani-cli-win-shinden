package pl.altani.spike.playback

import android.content.Intent
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.lifecycle.Lifecycle
import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.MainActivity
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.ui.PlaybackViewModel

/** The consent button on the technical screen, and Home or the lock while transfers are held, on the real Activity. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class ConsentUiTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val automation = InstrumentationRegistry.getInstrumentation().uiAutomation

    @Before
    fun setUp() {
        requireControlledServer()
        File(app.filesDir, "controlled-content/app").deleteRecursively()
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        network("wifi")
    }

    // the app's runtime lives as long as the process, so the next test must not start on mobile data
    @After
    fun tearDown() = network("wifi")

    private fun network(kind: String) {
        val reply = runBlocking { app.controlledBridge!!.call("network_set", JSONObject().put("kind", kind), mutation = true) }
        assertTrue("network_set $kind: $reply", reply is BridgeResult.Ok)
    }

    private fun ActivityScenario<MainActivity>.model(): PlaybackViewModel {
        var model: PlaybackViewModel? = null
        onActivity { model = it.playback }
        return model!!
    }

    private fun ActivityScenario<MainActivity>.bringToFront() {
        val intent = Intent(app, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
        app.startActivity(intent)
        val deadline = SystemClock.elapsedRealtime() + 10_000
        while (state != Lifecycle.State.RESUMED) {
            assertTrue("activity not resumed, still $state", SystemClock.elapsedRealtime() < deadline)
            Thread.sleep(100)
        }
    }

    /** Compose publishes its semantics to accessibility, as virtual nodes that a text search does not reach. */
    private fun node(text: String): AccessibilityNodeInfo? = find(automation.rootInActiveWindow, text)

    private fun find(node: AccessibilityNodeInfo?, text: String): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.text?.toString() == text && node.isVisibleToUser) return node
        return (0 until node.childCount).firstNotNullOfOrNull { find(node.getChild(it), text) }
    }

    private fun awaitShown(text: String, timeoutMs: Long = 10_000): AccessibilityNodeInfo {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (true) {
            node(text)?.let { return it }
            if (SystemClock.elapsedRealtime() > deadline) fail("\"$text\" not shown in ${timeoutMs}ms")
            Thread.sleep(100)
        }
    }

    private fun awaitGone(text: String) {
        val deadline = SystemClock.elapsedRealtime() + 10_000
        while (node(text) != null) {
            assertTrue("\"$text\" still shown", SystemClock.elapsedRealtime() < deadline)
            Thread.sleep(100)
        }
    }

    private fun tap(text: String) {
        val target = awaitShown(text)
        val clickable = generateSequence(target) { it.parent }.firstOrNull { it.isClickable } ?: target
        assertTrue("tap on \"$text\"", clickable.performAction(AccessibilityNodeInfo.ACTION_CLICK))
    }

    private fun startHeld(activity: ActivityScenario<MainActivity>): PlaybackController {
        val model = activity.model()
        val controller = requireNotNull(model.controller) { "controlled playback needs a debug build" }
        runBlocking { model.playControlled(scenario("hls", "segment_delay_ms" to 1_000)).join() }
        awaitOnMain("6 s buffered", 40_000, { controller.player.isPlaying to controller.player.totalBufferedDuration }) { (playing, ahead) ->
            playing && ahead >= 6_000
        }
        network("cellular")
        awaitOnMain("gate closed", 10_000, { controller.state.value.consentRequired }) { it }
        awaitShown(ALLOW)
        awaitOnMain("held once the buffer played out", 45_000, { controller.state.value }) { state ->
            check(state.phase != PlaybackController.Phase.STOPPED) { "stopped: ${state.error}" }
            state.phase == PlaybackController.Phase.WAITING_CONSENT
        }
        return controller
    }

    private fun PlaybackController.assertStaysPaused() {
        val until = SystemClock.elapsedRealtime() + 3_000
        while (SystemClock.elapsedRealtime() < until) {
            assertFalse("started on its own", onMain { player.playWhenReady })
            assertEquals(PlaybackController.Phase.PAUSED, onMain { state.value.phase })
            Thread.sleep(200)
        }
    }

    @Test
    fun theConsentButtonResumesThisEpisodeAndTheNextEpisodeAsksAgain() {
        ActivityScenario.launch(MainActivity::class.java).use { activity ->
            val controller = startHeld(activity)
            val requests = mediaRequests().size
            tap(ALLOW)
            awaitGone(ALLOW)
            awaitOnMain("playing again", 10_000, { controller.player.isPlaying }) { it }
            val from = onMain { controller.player.currentPosition }
            awaitOnMain("position moves on", 15_000, { controller.player.currentPosition }) { it >= from + 3_000 }
            assertTrue("transfers resumed", mediaRequests().size > requests)
            val first = onMain { controller.playbackSession }!!.getString("id")

            tap(NEXT_EPISODE)
            awaitOnMain("next episode waits", 15_000, { controller.state.value to controller.playbackSession }) { (state, session) ->
                session != null && session.getString("id") != first && state.phase == PlaybackController.Phase.WAITING_CONSENT
            }
            awaitShown(ALLOW)
            Thread.sleep(2_000)
            val log = ControlledEnvironment.serverLog()
            assertEquals("the consent stayed with the first episode", 0 to 0, log.getInt("resolves") to log.getJSONArray("requests").length())

            tap(ALLOW)
            awaitGone(ALLOW)
            awaitOnMain("next episode plays", 20_000, { controller.player.isPlaying }) { it }
            assertNotEquals(first, onMain { controller.playbackSession }!!.getString("id"))
        }
    }

    @Test
    fun homeWhileHeldKeepsPlaybackPausedBackOnWifi() {
        ActivityScenario.launch(MainActivity::class.java).use { activity ->
            val controller = startHeld(activity)
            shell("input keyevent KEYCODE_HOME")
            awaitOnMain("paused by Home", 5_000, { controller.state.value.phase }) { it == PlaybackController.Phase.PAUSED }
            activity.bringToFront()
            network("wifi")
            awaitGone(ALLOW)
            controller.assertStaysPaused()
            onMain { controller.resume() }
            awaitOnMain("playing after the user's play", 10_000, { controller.player.isPlaying }) { it }
        }
    }

    @Test
    fun theLockWhileHeldKeepsPlaybackPausedAfterTheConsentButton() {
        ActivityScenario.launch(MainActivity::class.java).use { activity ->
            val controller = startHeld(activity)
            shell("input keyevent KEYCODE_SLEEP")
            awaitOnMain("paused by the lock", 5_000, { controller.state.value.phase }) { it == PlaybackController.Phase.PAUSED }
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            activity.bringToFront()
            tap(ALLOW)
            awaitGone(ALLOW)
            controller.assertStaysPaused()
        }
    }

    private companion object {
        const val ALLOW = "Allow mobile data for this episode"
        const val NEXT_EPISODE = "HLS, next episode"
    }
}
