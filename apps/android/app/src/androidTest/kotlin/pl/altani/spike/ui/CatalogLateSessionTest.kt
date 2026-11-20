package pl.altani.spike.ui

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeEndpoint
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.playback.awaitOnMain
import pl.altani.spike.playback.onMain
import pl.altani.spike.playback.requireControlledServer
import pl.altani.spike.playback.scenario

/** The first `open_episode` has opened its session in Python, but its reply is held until the user picked another one. */
@RunWith(AndroidJUnit4::class)
class CatalogLateSessionTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val name = "late-session-${UUID.randomUUID()}"
    private val returned = CountDownLatch(1)
    private val release = CountDownLatch(1)

    @Volatile
    private var holdNext = true
    private lateinit var runtime: BridgeRuntime

    @Before
    fun setUp() {
        requireControlledServer()
        ControlledEnvironment.configure(scenario("hls"))
        runtime = ControlledEnvironment.runtime(app, name, wrap = ::holdReply)
    }

    @After
    fun tearDown() {
        release.countDown()
        scope.cancel()
        if (::runtime.isInitialized) assertTrue("runtime did not end", runBlocking { runtime.shutdown(10_000) }.ended)
        File(app.filesDir, "controlled-content/$name").deleteRecursively()
    }

    private fun holdReply(inner: BridgeEndpoint): BridgeEndpoint =
        object : BridgeEndpoint by inner {
            override fun call(method: String, payloadJson: String): String {
                val reply = inner.call(method, payloadJson)
                if (method == "open_episode" && holdNext) {
                    holdNext = false
                    returned.countDown()
                    check(release.await(30, TimeUnit.SECONDS)) { "reply held too long" }
                }
                return reply
            }
        }

    private fun sessions(): Int {
        val reply = runBlocking { runtime.call("engine_status") }
        return ((reply as BridgeResult.Ok).data).getInt("sessions")
    }

    @Test
    fun aSessionWhoseReplyCameAfterTheCancelIsClosed() {
        val flow = onMain {
            CatalogFlow(scope, { method, args -> runtime.call(method, args) }, lateResults = runtime.tracker.lateResults)
        }
        onMain { flow.search("frieren") }
        val hit = awaitOnMain("results", 20_000, { flow.state.value.search }) { it is CatalogFlow.Load.Ready }
        onMain { flow.selectSeries((hit as CatalogFlow.Load.Ready).value.hits.first().ref()) }
        val listed = awaitOnMain("episodes", 20_000, { flow.state.value.episodes }) { it is CatalogFlow.Load.Ready }
        val (first, second) = (listed as CatalogFlow.Load.Ready).value.episodes

        onMain { flow.openEpisode(first) }
        assertTrue("first open_episode never returned", returned.await(20, TimeUnit.SECONDS))
        assertEquals("Python already holds the first session", 1, sessions())

        onMain { flow.openEpisode(second) }
        release.countDown()

        val shown = awaitOnMain("second sources", 20_000, { flow.state.value.sources }) { it is CatalogFlow.Load.Ready }
        assertEquals(second, (shown as CatalogFlow.Load.Ready).value.episode)
        awaitOnMain("orphan closed", 10_000, ::sessions) { it == 1 }
        val reply = runBlocking { runtime.call("close_session", JSONObject().put("session_id", shown.value.sessionId)) }
        assertTrue("the second session is the one left", (reply as BridgeResult.Ok).data.getBoolean("closed"))
        assertEquals(0, sessions())
    }
}
