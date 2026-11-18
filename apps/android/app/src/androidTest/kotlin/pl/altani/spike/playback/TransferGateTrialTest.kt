package pl.altani.spike.playback

import android.os.SystemClock
import android.util.Log
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ApplicationProvider
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment

/** T9 trial (`-e t9trial true`): every hold candidate through the §5.4 cases, logged under `T9`, judged by no assert. */
@UnstableApi
@RunWith(Parameterized::class)
class TransferGateTrialTest(private val candidate: Candidate) {
    companion object {
        @JvmStatic
        @Parameterized.Parameters(name = "{0}")
        fun candidates(): List<Array<Any>> = Candidate.entries.map { arrayOf(it) }

        private const val SEGMENT = "hls/seg_\\d+\\.ts"
        private const val RESUME_LIMIT_MS = 10_000L
    }

    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val gate = TransferGate()
    private val probe = TransferProbe(gate)
    private val names = mutableListOf<String>()
    private var runtime: BridgeRuntime? = null
    private var controller: PlaybackController? = null

    @Before
    fun setUp() {
        assumeTrue("the T9 trial runs with -e t9trial true", InstrumentationRegistry.getArguments().getString("t9trial") == "true")
        requireControlledServer()
    }

    @After
    fun tearDown() {
        controller?.let { onMain { it.release() } }
        scope.cancel()
        runtime?.let { runBlocking { it.shutdown(10_000) } }
        names.forEach { File(app.filesDir, "controlled-content/$it").deleteRecursively() }
    }

    private fun start(scenario: JSONObject, stuckBufferingTimeoutMs: Int? = null): PlaybackController {
        val name = "t9-${UUID.randomUUID()}".also { names += it }
        val rt = ControlledEnvironment.runtime(app, name).also { runtime = it }
        ControlledEnvironment.configure(scenario)
        val listing = runBlocking { ControlledEnvironment.listing(rt) }
        val created = onMain { PlaybackController(
                app,
                rt,
                scope,
                {},
                gate = gate,
                hold = candidate.hold(gate, probe),
                manageGate = false,
                stuckBufferingTimeoutMs = stuckBufferingTimeoutMs,
            )
        }
        controller = created
        onMain { created.start(listing.series, listing.episode) }
        return created
    }

    private class View(val position: Long, val buffered: Long, val playing: Boolean, val state: Int, val error: String?)

    private fun PlaybackController.view(): View =
        onMain { View(player.currentPosition, player.bufferedPosition, player.isPlaying, player.playbackState, state.value.error) }

    /** Polls without failing: the last view, whether or not [done] was reached. */
    private fun PlaybackController.poll(timeoutMs: Long, done: (View) -> Boolean): View {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (true) {
            val view = view()
            if (done(view) || view.error != null || SystemClock.elapsedRealtime() > deadline) return view
            Thread.sleep(100)
        }
    }

    private fun PlaybackController.require(what: String, timeoutMs: Long, done: (View) -> Boolean) {
        val view = poll(timeoutMs, done)
        assertTrue("$what not reached (error ${view.error})", done(view))
    }

    /** Opens the gate; ms until playing again, or -1 when it does not within the limit. */
    private fun PlaybackController.reopen(): Long {
        val opened = SystemClock.elapsedRealtime()
        gate.open()
        val view = poll(RESUME_LIMIT_MS) { it.playing }
        return if (view.playing) SystemClock.elapsedRealtime() - opened else -1
    }

    private fun report(case: String, controller: PlaybackController, details: JSONObject) {
        val opens = probe.all()
        val result = JSONObject()
            .put("candidate", candidate.name)
            .put("case", case)
            .put("violations", JSONArray(opens.filter { it.violation }.map { it.toString() }))
            .put("raced", JSONArray(opens.filter { it.raced }.map { it.toString() }))
            .put("error", controller.view().error ?: JSONObject.NULL)
            .put("details", details)
        Log.i("T9", result.toString())
    }

    private fun bufferedAhead(view: View) = view.buffered - view.position

    private fun PlaybackController.drain(holdMs: Long): JSONObject {
        val closedAt = view().position
        val drained = poll(30_000) { it.state == Player.STATE_BUFFERING }
        val held = poll(holdMs) { false }
        return JSONObject()
            .put("played_after_close_ms", drained.position - closedAt)
            .put("drained", drained.state == Player.STATE_BUFFERING)
            .put("error_while_closed", held.error ?: JSONObject.NULL)
    }

    @Test
    fun case1IdleCloseThenResume() {
        val c = start(scenario("hls", "segment_delay_ms" to 1_000))
        c.require("6 s buffered", 40_000) { it.playing && bufferedAhead(it) >= 6_000 }
        gate.close()
        val details = c.drain(holdMs = 8_000)
        details.put("resume_ms", c.reopen())
        report("1_idle", c, details)
    }

    private fun race(point: TransferProbe.Point, case: String) {
        val c = start(scenario("hls", "segment_delay_ms" to 1_000))
        c.require("playing 2 s", 30_000) { it.position >= 2_000 }
        val pause = probe.pauseAt(point, Regex(SEGMENT))
        assertTrue("no segment open reached the pause", pause.reached.await(20, TimeUnit.SECONDS))
        val before = probe.all().lastOrNull()?.seq ?: 0
        gate.close()
        pause.release.countDown()
        c.poll(3_000) { false }
        val paused = pause.open ?: probe.all().firstOrNull { it.seq > before }
        val details = JSONObject().put("paused_open", paused?.toString() ?: JSONObject.NULL)
        details.put("resume_ms", c.reopen())
        report(case, c, details)
    }

    @Test
    fun case2aCloseJustBeforeTheCheck() = race(TransferProbe.Point.BEFORE_CHECK, "2a_race_before_check")

    @Test
    fun case2bCloseJustAfterTheCheck() = race(TransferProbe.Point.AFTER_CHECK, "2b_race_after_check")

    @Test
    fun case3RedirectAnsweredAfterTheClose() {
        val file = "hls/seg_004.ts"
        val c = start(scenario("hls", "redirect_files" to JSONArray().put(file), "redirect_delay_ms" to 3_000))
        val deadline = SystemClock.elapsedRealtime() + 30_000
        while (mediaRequests().none { it.getString("file") == file && it.getInt("status") == 302 }) {
            assertTrue("no redirect requested", SystemClock.elapsedRealtime() < deadline)
            Thread.sleep(50)
        }
        gate.close()
        c.poll(8_000) { false }
        val target = mediaRequests().firstOrNull { it.getString("file") == file && it.getBoolean("redirected") }
        val details = JSONObject()
            .put("target_after_close", target != null)
            .put("target", target?.let { JSONObject().put("probe", it.opt("probe")).put("status", it.opt("status")) } ?: JSONObject.NULL)
        details.put("resume_ms", c.reopen())
        report("3_redirect", c, details)
    }

    @Test
    fun case4RetryScheduledBeforeTheClose() {
        val file = "hls/seg_004.ts"
        val failures = AtomicInteger()
        probe.onResult = { open, code -> if (open.file == file && code == 503 && failures.incrementAndGet() == 2) gate.close() }
        val c = start(scenario("hls", "fail_files" to JSONObject().put(file, 2)))
        c.require("second 503", 30_000) { !gate.isOpen }
        c.poll(4_000) { false }
        val attempts = probe.all().filter { it.file == file }
        val details = JSONObject().put("attempts_while_closed", JSONArray(attempts.drop(2).map { it.toString() }))
        details.put("resume_ms", c.reopen())
        val servedNow = { mediaRequests().any { it.getString("file") == file && it.getInt("status") == 200 } }
        c.poll(RESUME_LIMIT_MS) { servedNow() }
        details.put("served_after_open", servedNow())
        report("4_retry", c, details)
    }

    @Test
    fun case5aLivePlaylistRefreshWhileClosed() {
        val c = start(scenario("hls", "live" to true))
        c.require("playing", 30_000) { it.playing }
        c.poll(2_000) { false }
        val requestsBefore = mediaRequests().size
        gate.close()
        val held = c.poll(7_000) { false }
        val playlists = mediaRequests().drop(requestsBefore).count { it.getString("file") == "hls/index.m3u8" }
        val details = JSONObject().put("requests_while_closed", mediaRequests().size - requestsBefore)
            .put("playlist_requests_while_closed", playlists)
            .put("error_while_closed", held.error ?: JSONObject.NULL)
        details.put("resume_ms", c.reopen())
        report("5a_live_refresh", c, details)
    }

    @Test
    fun case5bPreparedWithTheGateClosed() {
        gate.close()
        val c = start(scenario("hls"))
        val held = c.poll(6_000) { false }
        val details = JSONObject().put("requests_while_closed", JSONArray(mediaRequests().map { it.getString("file") }))
            .put("error_while_closed", held.error ?: JSONObject.NULL)
        details.put("resume_ms", c.reopen())
        report("5b_closed_start", c, details)
    }

    @Test
    fun case6TransferInFlightAtTheClose() {
        val file = "hls/seg_001.ts"
        probe.onResult = { open, code -> if (open.file == file && code == null) gate.close() }
        val c = start(scenario("hls", "throttle_bps" to 2_048))
        c.require("the close during $file", 60_000) { !gate.isOpen }
        val deadline = SystemClock.elapsedRealtime() + 20_000
        var entry: JSONObject?
        while (true) {
            entry = mediaRequests().firstOrNull { it.getString("file") == file && it.has("done_at") }
            if (entry != null || SystemClock.elapsedRealtime() > deadline) break
            Thread.sleep(100)
        }
        val details = JSONObject().put(
            "in_flight",
            entry?.let { JSONObject().put("sent", it.opt("sent")).put("size", it.opt("size")).put("aborted", it.opt("aborted")) }
                ?: JSONObject.NULL,
        )
        c.poll(2_000) { false }
        details.put("resume_ms", c.reopen())
        report("6_in_flight", c, details)
    }

    /** Media3's no-progress detector, shortened to 3 s: what a long wait for consent does without the controller's pause. */
    @Test
    fun case9LongHoldWithoutThePause() {
        val c = start(scenario("hls", "segment_delay_ms" to 1_000), stuckBufferingTimeoutMs = 3_000)
        c.require("6 s buffered", 40_000) { it.playing && bufferedAhead(it) >= 6_000 }
        gate.close()
        val details = c.drain(holdMs = 6_000)
        details.put("resume_ms", c.reopen())
        report("9_long_hold", c, details)
    }

    private fun blocked(): List<TransferProbe.Open> = probe.all().filter { !it.start.open && it.send == null && it.outcome == null }

    @Test
    fun case8CancelAndReleaseWhileHeld() {
        val c = start(scenario("hls", "segment_delay_ms" to 1_000))
        c.require("playing 2 s", 30_000) { it.position >= 2_000 }
        gate.close()
        val deadline = SystemClock.elapsedRealtime() + 15_000
        while (blocked().isEmpty() && SystemClock.elapsedRealtime() < deadline) Thread.sleep(100)
        val heldBeforeSeek = blocked()
        onMain { c.seekTo(c.player.currentPosition + 10_000) }
        c.poll(2_000) { false }
        val afterSeek = heldBeforeSeek.map { it.toString() }
        val heldBeforeRelease = blocked()
        val released = SystemClock.elapsedRealtime()
        onMain { c.release() }
        val releaseMs = SystemClock.elapsedRealtime() - released
        controller = null
        val unblockDeadline = SystemClock.elapsedRealtime() + 3_000
        while (heldBeforeRelease.any { it.outcome == null } && SystemClock.elapsedRealtime() < unblockDeadline) Thread.sleep(50)
        val details = JSONObject()
            .put("held_before_seek", heldBeforeSeek.size)
            .put("after_seek", JSONArray(afterSeek))
            .put("held_before_release", heldBeforeRelease.size)
            .put("release_ms", releaseMs)
            .put("after_release", JSONArray(heldBeforeRelease.map { it.toString() }))
        val result = JSONObject().put("candidate", candidate.name).put("case", "8_cancel_release").put("details", details)
        Log.i("T9", result.toString())
    }
}
