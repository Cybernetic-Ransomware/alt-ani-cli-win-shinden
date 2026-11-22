package pl.altani.spike.playback

import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.util.UUID
import kotlin.math.abs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
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

/** T7 and T8: Kotlin → bridge → `playback_prepare`/`stream_refresh` → Media3, against the controlled server. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class ControlledPlaybackTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val names = mutableListOf<String>()
    private val log = mutableListOf<String>()
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

    private fun start(
        scenario: JSONObject,
        storage: ControlledEnvironment.Storage = ControlledEnvironment.Storage.FS,
        criteria: JSONObject? = null,
        download: Boolean = false,
    ): PlaybackController {
        val name = "it-${UUID.randomUUID()}".also { names += it }
        val rt = ControlledEnvironment.runtime(app, name, storage).also { runtime = it }
        ControlledEnvironment.configure(scenario)
        val listing = runBlocking { ControlledEnvironment.listing(rt) }
        if (download) {
            runBlocking { ControlledEnvironment.download(rt, listing) }
            // a fresh log: whatever the player fetches from here on is in it
            ControlledEnvironment.configure(scenario)
        }
        val created = onMain { PlaybackController(app, rt, scope, { line -> synchronized(log) { log += line } }) }
        controller = created
        onMain { created.start(listing.series, listing.episode, criteria) }
        return created
    }

    private fun PlaybackController.awaitPosition(atLeastMs: Long, timeoutMs: Long = 45_000) =
        awaitOnMain("position >= $atLeastMs", timeoutMs, { player.currentPosition to state.value }) { (position, state) ->
            check(state.phase != PlaybackController.Phase.STOPPED) { "stopped: ${state.error}" }
            position >= atLeastMs
        }

    private fun assertEveryMediaRequestCarriedTheHeaders(requests: List<JSONObject>) {
        val refused = requests.filter { it.getJSONArray("missing").length() > 0 }
        assertTrue("requests without the variant's headers: $refused", refused.isEmpty())
    }

    private fun playsOnline(kind: String, vararg expectedFiles: Regex) {
        val player = start(scenario(kind))
        player.awaitPosition(10_000)
        assertEquals("online", onMain { player.state.value.mode })
        val requests = mediaRequests()
        assertEveryMediaRequestCarriedTheHeaders(requests)
        assertTrue("refused: $requests", requests.all { it.getInt("status") in setOf(200, 206) })
        for (pattern in expectedFiles) {
            assertTrue("no request for $pattern in $requests", requests.any { pattern.matches(it.getString("file")) })
        }
        assertEquals(1, ControlledEnvironment.serverLog().getInt("resolves"))
    }

    @Test
    fun progressivePlaysWithTheVariantHeaders() = playsOnline("progressive", Regex("video\\.mp4"))

    @Test
    fun hlsSendsTheHeadersWithThePlaylistAndEverySegment() =
        playsOnline("hls", Regex("hls/index\\.m3u8"), Regex("hls/seg_00[0-4]\\.ts"))

    @Test
    fun staticDashPlays() =
        playsOnline("dash", Regex("dash/manifest\\.mpd"), Regex("dash/init\\.m4s"), Regex("dash/chunk_\\d+\\.m4s"))

    private fun playsLocal(storage: ControlledEnvironment.Storage, kind: String) {
        val player = start(scenario("progressive"), storage, download = true)
        player.awaitPosition(10_000)
        assertEquals(kind, onMain { player.state.value.mode })
        assertEquals("no network while playing a local copy", emptyList<JSONObject>(), mediaRequests())
    }

    @Test
    fun downloadedEpisodePlaysFromAFile() = playsLocal(ControlledEnvironment.Storage.FS, "file")

    @Test
    fun downloadedEpisodePlaysThroughAContentUri() = playsLocal(ControlledEnvironment.Storage.CONTENT_URI, "content_uri")

    /** The first URL expires after a few seconds while slow segments keep the buffer short. */
    private fun expiring(vararg extra: Pair<String, Any?>) = scenario("hls", "ttl_sec" to 6, "segment_delay_ms" to 1_000, *extra)

    private fun labels(vararg values: String) = JSONArray(values.toList())

    private fun expiredAt(): Long =
        synchronized(log) { log.single { it.startsWith("url expired") } }.substringAfter(" at ").substringBefore("ms").toLong()

    /** The position right after playback resumed from the one refresh. */
    private fun PlaybackController.awaitRefreshed(): Long {
        awaitOnMain("one refresh", 30_000, { state.value }) { it.refreshes == 1 && it.phase == PlaybackController.Phase.PLAYING }
        return onMain { player.currentPosition }
    }

    @Test
    fun expiredUrlIsRefreshedOnceAndPlaybackResumesAtThePosition() {
        val player = start(expiring())
        val resumedAt = player.awaitRefreshed()
        val expiredAt = expiredAt()
        assertTrue("resumed at $resumedAt, expired at $expiredAt", abs(resumedAt - expiredAt) <= 2_000)
        player.awaitPosition(expiredAt + 4_000)
        val state = onMain { player.state.value }
        assertEquals(1 to 0, state.refreshes to state.variantChanges)
        val requests = mediaRequests()
        assertTrue("no expired request in $requests", requests.any { it.optString("reason") == "expired" })
        assertEveryMediaRequestCarriedTheHeaders(requests)
        assertEquals(2, ControlledEnvironment.serverLog().getInt("resolves"))
        val saved = onMain { player.writes.value }.map { it.first }.filter { it.method == PositionReporter.POSITION }
        assertTrue("the position at the expiry was not saved: $saved", saved.any { it.positionMs == expiredAt })
    }

    @Test
    fun aRefreshToAnotherFittingVariantKeepsPlaying() {
        val criteria = JSONObject().put("quality", "max:720")
        val player = start(expiring("variants" to labels("720p", "480p"), "refresh_variants" to labels("480p")), criteria = criteria)
        assertEquals("720p", awaitOnMain("variant", 15_000, { player.state.value.variant }) { it != null })
        val resumedAt = player.awaitRefreshed()
        player.awaitPosition(resumedAt + 3_000)
        val state = onMain { player.state.value }
        assertEquals(1 to "480p", state.variantChanges to state.variant)
        assertNull(state.error)
    }

    @Test
    fun aRefreshWithNothingWithinTheLimitStopsWithTheBridgeCode() {
        val criteria = JSONObject().put("quality", "max:720")
        val player = start(expiring("variants" to labels("720p"), "refresh_variants" to labels("1080p")), criteria = criteria)
        val state = awaitOnMain("stop", 30_000, { player.state.value }) { it.phase == PlaybackController.Phase.STOPPED }
        assertEquals("criteria.no_match", state.error)
        assertEquals(1, state.refreshes)
        assertEquals(2, ControlledEnvironment.serverLog().getInt("resolves"))
    }

    @Test
    fun aUrlThatKeepsExpiringStopsAfterOneRefresh() {
        val player = start(expiring("refresh_ttl_sec" to 4))
        val state = awaitOnMain("stop", 45_000, { player.state.value }) { it.phase == PlaybackController.Phase.STOPPED }
        assertEquals("refresh_limit", state.error)
        assertEquals(1, state.refreshes)
        assertEquals(2, ControlledEnvironment.serverLog().getInt("resolves"))
    }
}
