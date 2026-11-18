package pl.altani.spike.live

import android.os.SystemClock
import android.util.Log
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import androidx.media3.common.Player
import androidx.media3.common.VideoSize
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.analytics.AnalyticsListener
import androidx.media3.ui.PlayerView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.chaquo.python.Python
import java.io.File
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.MainActivity
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.bridge.ChaquopyEndpoint
import pl.altani.spike.playback.PlaybackController
import pl.altani.spike.playback.awaitOnMain
import pl.altani.spike.playback.onMain
import pl.altani.spike.playback.shell

/** T12 and T3b against the real shinden.pl, opt-in with `-e shindenLive true`; [TAG] lines carry no URL, cookie or token. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class ShindenLiveTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val args = InstrumentationRegistry.getArguments()
    private val dirs = mutableListOf<File>()
    private val runtimes = mutableListOf<BridgeRuntime>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var controller: PlaybackController? = null

    private val solver: String? = args.getString("flaresolverr")?.takeIf { it.isNotBlank() }
    private val query: String = args.getString("query") ?: "frieren"

    @Before
    fun setUp() = assumeTrue("live Shinden checks need -e shindenLive true", args.getString("shindenLive") == "true")

    @After
    fun tearDown() {
        controller?.let { onMain { it.release() } }
        scope.cancel()
        runBlocking { runtimes.forEach { it.shutdown(10_000) } }
        // the cache holds a cf_clearance cookie when the solver was used
        dirs.forEach { it.deleteRecursively() }
    }

    private fun runtime(useSolver: Boolean): BridgeRuntime {
        val dir = File(app.filesDir, "shinden-live/${UUID.randomUUID()}").also { dirs += it; it.mkdirs() }
        val access = JSONObject().put("use_flaresolverr", useSolver)
        if (useSolver) access.put("flaresolverr_url", solver)
        return BridgeRuntime(
            endpointFactory = { ChaquopyEndpoint(Python.getInstance().getModule("android_host").callAttr("create_bridge")) },
            engineArgs = {
                JSONObject()
                    .put("cache_dir", File(dir, "cache").absolutePath)
                    .put("history_path", File(dir, "state.db").absolutePath)
                    .put("access", access)
                    .put("network", "wifi")
            },
        ).also { runtimes += it }
    }

    private fun report(kind: String, data: JSONObject) = Log.i(TAG, "$kind ${data.put("kind", kind)}")

    private fun BridgeResult.outcome(): JSONObject =
        when (this) {
            is BridgeResult.Ok -> JSONObject().put("ok", true)
            is BridgeResult.Failure -> JSONObject().put("ok", false).put("code", code).put("error_kind", kind).put("params", params)
            BridgeResult.NotStarted -> JSONObject().put("ok", false).put("code", "not_started")
        }

    private class Timed(val result: BridgeResult, val ms: Long)

    private fun BridgeRuntime.timed(method: String, payload: JSONObject): Timed {
        val start = SystemClock.elapsedRealtime()
        val result = runBlocking { call(method, payload) }
        return Timed(result, SystemClock.elapsedRealtime() - start)
    }

    private class Listing(val series: JSONObject, val episodes: JSONArray, val complete: Boolean)

    /** search → episodes with one report line per step; null when a step failed. */
    private fun listing(rt: BridgeRuntime, column: String): Listing? {
        val found = rt.timed("search", JSONObject().put("query", query))
        val searched = found.result.outcome().put("column", column).put("step", "search").put("ms", found.ms)
        val hits = (found.result as? BridgeResult.Ok)?.data?.getJSONArray("hits")
        searched.put("hits", hits?.length())
        report("T12_STEP", searched)
        val hit = hits?.let { all -> (0 until all.length()).map { all.getJSONObject(it) }.firstOrNull { "/series/" in it.getString("url") } }
            ?: return null
        val ref = JSONObject()
        listOf("id", "slug", "title", "url").forEach { ref.put(it, hit.get(it)) }
        val listed = rt.timed("episodes", JSONObject().put("series", ref))
        val data = (listed.result as? BridgeResult.Ok)?.data
        report(
            "T12_STEP",
            listed.result.outcome().put("column", column).put("step", "episodes").put("ms", listed.ms)
                .put("episodes", data?.getJSONArray("episodes")?.length())
                .put("complete", data?.opt("complete"))
                .put("pages_fetched", data?.opt("pages_fetched")),
        )
        return data?.let { Listing(it.getJSONObject("series"), it.getJSONArray("episodes"), it.optBoolean("complete", false)) }
    }

    /** T12 transport columns on the current network: curl_cffi straight to Shinden and, given a solver, with its clearance. */
    @Test
    fun transportColumns() {
        val columns = listOfNotNull("direct" to false, solver?.let { "flaresolverr" to true })
        val results = columns.map { (column, useSolver) -> column to (listing(runtime(useSolver), column)?.complete == true) }
        report("T12_TRANSPORT", JSONObject().put("network", "wifi").put("columns", JSONObject(results.toMap())))
        // every configured column must pass on its own, so a working solver cannot hide a broken direct path
        results.forEach { (column, complete) -> assertTrue("$column did not reach a complete episode list", complete) }
    }

    /** T3b on the device: the stdlib backend's time for one full episode-list page, p95 of repeated runs. */
    @Test
    fun episodeListParseTime() {
        val py = Python.getInstance()
        val scope = py.builtins.callAttr("dict")
        scope.callAttr("__setitem__", "QUERY", query)
        py.builtins.callAttr("exec", PARSE_TIME_SCRIPT, scope)
        val result = JSONObject(scope.callAttr("__getitem__", "RESULT").toString())
        report("T3B_DEVICE", result)
        assertTrue("p95 ${result.getDouble("p95_ms")} ms", result.getDouble("p95_ms") < 300.0)
    }

    /** Shinden → resolve → Media3 for [playSeconds] on screen, with decoder counters as evidence beyond "no exception". */
    @Test
    fun fullFlowPlays() {
        val useSolver = args.getString("access") == "flaresolverr"
        assumeTrue("access=flaresolverr needs -e flaresolverr <url>", !useSolver || solver != null)
        val column = if (useSolver) "flaresolverr" else "direct"
        val playSeconds = args.getString("playSeconds")?.toLong() ?: 30L
        val rt = runtime(useSolver)
        val listing = listing(rt, column) ?: return fail("no episode list ($column), see the T12_STEP lines")
        val index = args.getString("episodeIndex")?.toInt() ?: 0
        val episode = listing.episodes.getJSONObject(index)

        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        ActivityScenario.launch(MainActivity::class.java).use { activity ->
            val evidence = Evidence()
            val log = mutableListOf<String>()
            val player = onMain {
                PlaybackController(app, rt, scope, { line -> synchronized(log) { log += line } }).also { controller = it }
            }
            activity.onActivity { screen ->
                screen.window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                val view = PlayerView(screen).apply { this.player = player.player }
                screen.addContentView(view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, screen.resources.displayMetrics.heightPixels / 2))
                player.player.addAnalyticsListener(evidence)
            }
            val started = SystemClock.elapsedRealtime()
            onMain { player.start(listing.series, episode) }
            val playing = runCatching {
                awaitOnMain("playing", 240_000, { player.state.value }) {
                    check(it.phase != PlaybackController.Phase.STOPPED) { "stopped: ${it.error}" }
                    it.phase == PlaybackController.Phase.PLAYING
                }
            }
            val state = onMain { player.state.value }
            val base = JSONObject()
                .put("column", column)
                .put("episode_number", episode.opt("number"))
                .put("prepare_to_playing_ms", SystemClock.elapsedRealtime() - started)
                .put("phase", state.phase.name)
                .put("error", state.error)
                .put("mode", state.mode)
                .put("variant", state.variant)
                .put("controller_log", JSONArray(synchronized(log) { log.toList() }))
            if (playing.isFailure) {
                report("T12_PLAY", base.put("result", "not_playing").put("stage", stage(state.error)))
                fail("no playback: ${state.error} (${stage(state.error)})")
            }
            val from = onMain { player.player.currentPosition }
            val counters0 = onMain { evidence.counters(player) }
            val wallStart = SystemClock.elapsedRealtime()
            val reached = runCatching {
                awaitOnMain("position +${playSeconds}s", playSeconds * 1000 + 60_000, { player.player.currentPosition to player.state.value }) { (position, s) ->
                    check(s.phase != PlaybackController.Phase.STOPPED) { "stopped: ${s.error}" }
                    check(evidence.seeks == 0) { "seeked during the run" }
                    position >= from + playSeconds * 1000 && SystemClock.elapsedRealtime() - wallStart >= playSeconds * 1000
                }
            }
            val to = onMain { player.player.currentPosition }
            val counters1 = onMain { evidence.counters(player) }
            val after = onMain { player.state.value }
            val videoFrames = counters1.getInt("video_rendered") - counters0.getInt("video_rendered")
            val seconds = (to - from) / 1000.0
            report(
                "T12_PLAY",
                base.put("result", if (reached.isSuccess) "played" else "interrupted")
                    .put("stage", if (reached.isSuccess) null else stage(after.error))
                    .put("phase_after", after.phase.name)
                    .put("error_after", after.error)
                    .put("refreshes", after.refreshes)
                    .put("played_s", seconds)
                    .put("wall_s", (SystemClock.elapsedRealtime() - wallStart) / 1000.0)
                    .put("video_frames_rendered", videoFrames)
                    .put("video_fps", if (seconds > 0) videoFrames / seconds else 0.0)
                    .put("counters_start", counters0)
                    .put("counters_end", counters1)
                    .put("evidence", evidence.summary()),
            )
            assertTrue("did not play ${playSeconds}s: ${after.error}", reached.isSuccess)
            assertTrue("no video frame rendered", evidence.firstFrame && videoFrames > 0)
            assertTrue("audio never advanced", evidence.audioAdvancing)
        }
    }

    /** Where in the chain a failure code comes from; the codes are the bridge's and the controller's own. */
    private fun stage(code: String?): String =
        when {
            code == null -> "none"
            code.startsWith("player:") -> "player"
            code == "access_blocked" || code.startsWith("interaction") -> "access_or_challenge"
            code.startsWith("xhr_") -> "shinden_api"
            code == "sources_exhausted" || code.contains("stream") || code.contains("host") -> "extractor_or_host"
            code.contains("parse") || code.contains("drift") -> "parser"
            code in setOf("network_error", "timeout", "http_status", "transport_unavailable") -> "transport"
            else -> "other"
        }

    @UnstableApi
    private class Evidence : AnalyticsListener {
        var firstFrame = false
        var audioAdvancing = false
        var seeks = 0
        private var videoSize: VideoSize? = null
        private var videoDecoder: String? = null
        private var audioDecoder: String? = null

        override fun onRenderedFirstFrame(eventTime: AnalyticsListener.EventTime, output: Any, renderTimeMs: Long) {
            firstFrame = true
        }

        override fun onAudioPositionAdvancing(eventTime: AnalyticsListener.EventTime, playoutStartSystemTimeMs: Long) {
            audioAdvancing = true
        }

        override fun onPositionDiscontinuity(
            eventTime: AnalyticsListener.EventTime,
            oldPosition: Player.PositionInfo,
            newPosition: Player.PositionInfo,
            reason: Int,
        ) {
            // a seek on the player view would otherwise pass for played time
            if (reason == Player.DISCONTINUITY_REASON_SEEK) seeks++
        }

        override fun onVideoSizeChanged(eventTime: AnalyticsListener.EventTime, videoSize: VideoSize) {
            this.videoSize = videoSize
        }

        override fun onVideoDecoderInitialized(eventTime: AnalyticsListener.EventTime, decoderName: String, initializedTimestampMs: Long, initializationDurationMs: Long) {
            videoDecoder = decoderName
        }

        override fun onAudioDecoderInitialized(eventTime: AnalyticsListener.EventTime, decoderName: String, initializedTimestampMs: Long, initializationDurationMs: Long) {
            audioDecoder = decoderName
        }

        fun counters(controller: PlaybackController): JSONObject {
            val video = controller.player.videoDecoderCounters?.also { it.ensureUpdated() }
            val audio = controller.player.audioDecoderCounters?.also { it.ensureUpdated() }
            return JSONObject()
                .put("video_rendered", video?.renderedOutputBufferCount ?: 0)
                .put("video_dropped", video?.droppedBufferCount ?: 0)
                .put("audio_rendered", audio?.renderedOutputBufferCount ?: 0)
        }

        fun summary(): JSONObject =
            JSONObject()
                .put("first_frame", firstFrame)
                .put("audio_advancing", audioAdvancing)
                .put("seeks", seeks)
                .put("video_size", videoSize?.let { "${it.width}x${it.height}" })
                .put("video_decoder", videoDecoder)
                .put("audio_decoder", audioDecoder)
    }

    private companion object {
        const val TAG = "ShindenLive"

        // runs in the app's interpreter; picks the longest page 1 among the first /series/ hits
        val PARSE_TIME_SCRIPT = """
import json, statistics, time
from alt_ani_core import dom
from alt_ani_core.dom import stdlib_backend
from alt_ani_core.shinden import search, series
from alt_ani_core.shinden.utils import ParseTally
from alt_ani_core.transport import clearance
from alt_ani_core.transport.curl import CurlTransport

dom.set_backend(stdlib_backend.parse)
with CurlTransport().open(clearance.shinden_profile(None)) as s:
    hits = [h for h in search.search_series(s, QUERY) if "/series/" in h.url][:4]
    pages = []
    for hit in hits:
        ref = series.parse_series_url(hit.url)
        resp = s.get(f"{ref.url}/episodes", params={"strona": 1, "na_stronie": series.EPISODES_PER_PAGE})
        if resp.status_code == 200:
            pages.append(resp.text)
html = max(pages, key=len)
times = []
rows = 0
for _ in range(30):
    start = time.perf_counter()
    rows = len(series._parse_page(html, ParseTally("episodes")).rows)
    times.append((time.perf_counter() - start) * 1000)
RESULT = json.dumps({
    "backend": "stdlib", "rows": rows, "bytes": len(html), "runs": len(times),
    "median_ms": round(statistics.median(times), 1), "p95_ms": round(statistics.quantiles(times, n=20)[-1], 1),
    "max_ms": round(max(times), 1),
})
""".trimIndent()
    }
}
