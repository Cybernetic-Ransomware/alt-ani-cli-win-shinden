package pl.altani.spike.playback

import android.content.Intent
import android.os.SystemClock
import androidx.lifecycle.Lifecycle
import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import kotlin.math.abs
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.MainActivity
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.ui.PlaybackViewModel

/** T10 on the real Activity: rotation keeps playing with the rewind, Home and the lock pause and save, the end marks watched. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class PlaybackLifecycleTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()

    @Before
    fun setUp() {
        requireControlledServer()
        // the app's controlled runtime opens lazily, on the first PlaybackViewModel; start from an empty watch state
        File(app.filesDir, "controlled-content/app").deleteRecursively()
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
    }

    private fun ActivityScenario<MainActivity>.model(): PlaybackViewModel {
        var model: PlaybackViewModel? = null
        onActivity { model = it.playback }
        return model!!
    }

    /** Back from Home or the lock the way the user comes back: the same instance, brought to the front. */
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

    private fun PlaybackController.awaitPosition(atLeastMs: Long) =
        awaitOnMain("position >= $atLeastMs", 30_000, { player.currentPosition }) { it >= atLeastMs }

    private fun watch(listing: ControlledEnvironment.Listing): JSONObject {
        val args = JSONObject()
            .put("series_id", listing.series.getString("id"))
            .put("numbers", JSONArray().put(listing.episode.get("number")))
        val reply = runBlocking { app.controlledBridge!!.call("watch_get", args) } as BridgeResult.Ok
        return reply.data.getJSONArray("episodes").getJSONObject(0)
    }

    /** The bridge answered a write of [method] near [positionMs]; the saved position then equals it within 1 s. */
    private fun PlaybackController.awaitWrite(method: String, positionMs: Long) =
        awaitOnMain("$method near $positionMs", 10_000, { writes.value }) { writes ->
            writes.any { (write, result) -> write.method == method && abs(write.positionMs - positionMs) <= 1_000 && result is BridgeResult.Ok }
        }

    @Test
    fun rotationKeepsPlayingWhileHomeAndTheLockPauseAndOnlyTheEndMarksWatched() {
        ActivityScenario.launch(MainActivity::class.java).use { activity ->
            val model = activity.model()
            val controller = requireNotNull(model.controller) { "controlled playback needs a debug build" }
            runBlocking { model.playControlled(scenario("hls")).join() }
            controller.awaitPosition(4_000)
            val listing = runBlocking { ControlledEnvironment.listing(app.controlledBridge!!) }
            val saved = mutableListOf(watch(listing).optLong("position_ms"))

            val beforeRotation = onMain { controller.player.currentPosition }
            val pausesBefore = onMain { controller.state.value.pauses }
            activity.recreate()
            assertSame("the session survives the recreation", controller, activity.model().controller)
            val rewind = onMain { controller.lastRewind }!!
            assertEquals(PlaybackController.ROTATION_REWIND_MS, rewind.fromMs - rewind.toMs)
            assertTrue("rewound from ${rewind.fromMs}, before $beforeRotation", rewind.fromMs >= beforeRotation)
            assertTrue(onMain { controller.player.playWhenReady })
            assertEquals("rotation never pauses", pausesBefore, onMain { controller.state.value.pauses })
            awaitOnMain("playing after the rotation", 3_000, { controller.player.isPlaying }) { it }
            controller.awaitPosition(rewind.fromMs + 1_000)

            shell("input keyevent KEYCODE_HOME")
            awaitOnMain("paused by Home", 5_000, { controller.player.playWhenReady }) { !it }
            val homeAt = onMain { controller.player.currentPosition }
            controller.awaitWrite(PositionReporter.POSITION, homeAt)
            saved += watch(listing).optLong("position_ms")
            assertTrue("saved ${saved.last()}, paused at $homeAt", abs(saved.last() - homeAt) <= 1_000)
            activity.bringToFront()
            assertFalse("coming back does not resume", onMain { controller.player.playWhenReady })

            onMain { controller.resume() }
            awaitOnMain("playing again", 5_000, { controller.player.isPlaying }) { it }
            shell("input keyevent KEYCODE_SLEEP")
            awaitOnMain("paused by the lock", 5_000, { controller.player.playWhenReady }) { !it }
            val lockAt = onMain { controller.player.currentPosition }
            controller.awaitWrite(PositionReporter.POSITION, lockAt)
            saved += watch(listing).optLong("position_ms")
            assertTrue("saved ${saved.last()}, locked at $lockAt", abs(saved.last() - lockAt) <= 1_000)
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            activity.bringToFront()

            assertFalse("not watched before the end", watch(listing).getBoolean("watched"))
            onMain {
                controller.seekTo(controller.player.duration - 3_000)
                controller.resume()
            }
            awaitOnMain("ended", 20_000, { controller.state.value.phase }) { it == PlaybackController.Phase.ENDED }
            val end = onMain { controller.player.duration }
            controller.awaitWrite(PositionReporter.ENDED, end)
            val final = watch(listing)
            assertTrue("watched after the end", final.getBoolean("watched"))
            saved += final.optLong("position_ms")
            assertEquals("the saved position never went back: $saved", saved.sorted(), saved)

            val writes = onMain { controller.writes.value }
            val seqs = writes.map { it.first.seq }.filter { it > 0 }
            assertEquals("writes answered in seq order", seqs.sorted().distinct(), seqs)
            val positions = writes.filter { it.first.method == PositionReporter.POSITION }.map { it.first.positionMs }
            assertEquals("no write moved the position back: $positions", positions.sorted(), positions)
            assertTrue(
                "every write accepted",
                writes.all { (_, result) -> result is BridgeResult.Ok && result.data.optBoolean("accepted", true) },
            )
        }
    }
}
