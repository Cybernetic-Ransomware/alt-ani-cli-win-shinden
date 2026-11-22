package pl.altani.spike.offline.background

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.MainActivity
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.offline.DeviceOutcomes
import pl.altani.spike.offline.PADDED_BYTES
import pl.altani.spike.offline.Runtimes
import pl.altani.spike.offline.awaitHeld
import pl.altani.spike.offline.heldScenario
import pl.altani.spike.offline.ok
import pl.altani.spike.offline.partials
import pl.altani.spike.offline.plainScenario
import pl.altani.spike.offline.releaseAfterTheMonitor
import pl.altani.spike.offline.VIDEO_BYTES
import pl.altani.spike.offline.waitUntil
import pl.altani.spike.playback.mediaRequests
import pl.altani.spike.playback.requireControlledServer
import pl.altani.spike.playback.shell

/** Runs the real component of this API level (UIDT from 34, the `dataSync` service below) on the test's own runtime. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class BackgroundRunTest {
    @get:Rule
    val outcomes = DeviceOutcomes()

    private val runtimes = Runtimes()
    private var activity: ActivityScenario<MainActivity>? = null

    @Before
    fun setUp() {
        requireControlledServer()
        endSessions()
    }

    @After
    fun tearDown() {
        endSessions()
        activity?.close()
        runtimes.closeAll()
    }

    private class Started(val runtime: BridgeRuntime, val name: String, val jobIds: List<String>)

    private fun start(scenario: JSONObject = heldScenario(), episodes: Int = 1, held: Boolean = true): Started {
        val name = runtimes.newName()
        val runtime = runtimes.open(name)
        app.backgroundControlled = runtime
        ControlledEnvironment.configure(scenario)
        val listing = (0 until episodes).map { runBlocking { ControlledEnvironment.listing(runtime, it) } }
        val jobIds = listing.map { runBlocking { ControlledEnvironment.enqueue(runtime, it) } }
        activity = ActivityScenario.launch(MainActivity::class.java)
        assertEquals(RequestResult.Launched, requestControlled())
        awaitStage(RunState.Stage.ACTIVE)
        if (held) {
            try {
                awaitHeld(runtimes.library(name), HELD_TIMEOUT_MS)
            } catch (e: IllegalStateException) {
                throw IllegalStateException("${e.message}; session=${app.offlineRun.state.value} executor=${executorState(runtime)}", e)
            }
        }
        return Started(runtime, name, jobIds)
    }

    private fun job(runtime: BridgeRuntime, jobId: String) = runBlocking { ControlledEnvironment.job(runtime, jobId) }

    /** Like `awaitJob`, but a retryable read (`history.unavailable`, a database busy past its timeout) is read again. */
    private fun await(runtime: BridgeRuntime, jobId: String, vararg states: String, timeoutMs: Long = 90_000): JSONObject {
        var job: JSONObject? = null
        waitUntil("job $jobId in ${states.toList()}", timeoutMs) {
            when (val reply = runBlocking { runtime.call("queue_job", JSONObject().put("job_id", jobId)) }) {
                is BridgeResult.Ok -> {
                    val state = reply.data.getJSONObject("job").also { job = it }.getString("state")
                    if (state !in states && state in setOf("failed", "cancelled")) throw AssertionError("job ended: ${job}")
                    state in states
                }
                is BridgeResult.Failure -> if (reply.retryable) false else throw AssertionError("queue_job failed: ${reply.code}")
                BridgeResult.NotStarted -> throw AssertionError("queue_job not started")
            }
        }
        return job!!
    }

    private fun revision(runtime: BridgeRuntime, jobId: String) =
        JSONObject().put("job_id", jobId).put("expected_revision", job(runtime, jobId).getInt("revision"))

    private fun resumedWithRange(): Boolean =
        mediaRequests().filter { it.getString("file") == "video.mp4" && it.getString("method") == "GET" && !it.isNull("range") }
            .map { it.getString("range") }
            .let { it.isNotEmpty() && "bytes=0-" !in it }

    @Test
    fun aTransferGoesOnAfterHome() {
        val s = start()
        shell("input keyevent KEYCODE_HOME")
        Thread.sleep(3_000)
        assertEquals(RunState.Stage.ACTIVE, app.offlineRun.state.value.stage)
        ControlledEnvironment.releaseStall()
        await(s.runtime, s.jobIds.single(), "completed")
        awaitComponentGone()
        assertOnePublishedCopy(s.runtime)
    }

    @Test
    fun aTransferGoesOnWithTheScreenOffAndTheDeviceIdle() {
        val s = start()
        try {
            shell("input keyevent KEYCODE_HOME")
            shellOutput("dumpsys battery unplug")
            shell("input keyevent KEYCODE_SLEEP")
            Thread.sleep(2_000)
            shellOutput("dumpsys deviceidle force-idle")
            Thread.sleep(3_000)
            assertEquals(RunState.Stage.ACTIVE, app.offlineRun.state.value.stage)
            ControlledEnvironment.releaseStall()
            await(s.runtime, s.jobIds.single(), "completed")
            awaitComponentGone()
        } finally {
            shellOutput("dumpsys deviceidle unforce")
            shellOutput("dumpsys battery reset")
            shell("input keyevent KEYCODE_WAKEUP")
        }
        assertOnePublishedCopy(s.runtime)
    }

    @Test
    fun recreatingTheActivityKeepsOneSessionOneEngineAndOneExecutor() {
        val s = start()
        val generation = app.offlineRun.state.value.generation
        repeat(10) { activity!!.recreate() }
        assertEquals(generation, app.offlineRun.state.value.generation)
        assertEquals(RunState.Stage.ACTIVE, app.offlineRun.state.value.stage)
        assertEquals(1, s.runtime.engineCreations)
        assertEquals("self", s.runtime.ok("executor_status").getString("owner"))
        assertEquals(RequestResult.AlreadyActive, requestControlled())
        ControlledEnvironment.releaseStall()
        await(s.runtime, s.jobIds.single(), "completed")
        awaitComponentGone()
        assertEquals(generation, app.offlineRun.state.value.generation)
    }

    @Test
    fun theNotificationStopDuringHeldIoKeepsOwnershipAndTheComponentUntilTheIoEnds() {
        val s = start()
        val generation = app.offlineRun.state.value.generation
        app.sendBroadcast(
            Intent(app, RunStopReceiver::class.java)
                .setAction(RunStopReceiver.ACTION_STOP)
                .putExtra(RunStopReceiver.EXTRA_GENERATION, generation),
        )
        awaitStage(RunState.Stage.DRAINING)
        waitUntil("executor closing") { executorState(s.runtime) == "closing" }
        val second = runBlocking { s.runtime.call("executor_start", mutation = true) }
        assertEquals("executor.closing", (second as BridgeResult.Failure).code)
        Thread.sleep(OfflineRun.STOP_TIMEOUT_MS + 2_000)
        assertTrue("component released before the I/O ended", PlatformLauncher(app).isActive())
        assertEquals("closing", executorState(s.runtime))
        ControlledEnvironment.releaseStall()
        awaitComponentGone()
        assertEquals("stopped", executorState(s.runtime))
        val stopped = job(s.runtime, s.jobIds.single())
        assertEquals("queued", stopped.getString("state"))
        assertEquals(0, stopped.getInt("retry_count"))
        assertTrue(stopped.getBoolean("has_partial"))
        assertEquals(1, partials(runtimes.library(s.name)).size)
    }

    @Test
    fun pausingTheOnlyJobEndsTheSessionAndKeepsItsPartial() {
        val s = start()
        val jobId = s.jobIds.single()
        s.runtime.ok("queue_pause", revision(s.runtime, jobId), mutation = true)
        releaseAfterTheMonitor()
        val paused = await(s.runtime, jobId, "paused")
        awaitComponentGone()
        assertTrue(paused.getLong("bytes_done") > 0)
        assertTrue(partials(runtimes.library(s.name)).single().length() >= paused.getLong("bytes_done"))
    }

    @Test
    fun cancellingOneJobKeepsTheSessionForTheNextAndRemovesOnlyItsPartial() {
        val s = start(episodes = 2)
        val (first, second) = s.jobIds
        s.runtime.ok("queue_cancel", revision(s.runtime, first), mutation = true)
        releaseAfterTheMonitor()
        await(s.runtime, first, "cancelled")
        await(s.runtime, second, "completed")
        awaitComponentGone()
        assertEquals(emptyList<Any>(), partials(runtimes.library(s.name)))
        assertOnePublishedCopy(s.runtime)
    }

    @Test
    fun goingOfflineHoldsTheJobInTheSessionAndTheNetworkComingBackResumesIt() {
        val s = start()
        val jobId = s.jobIds.single()
        s.runtime.ok("network_set", JSONObject().put("kind", "offline"), mutation = true)
        releaseAfterTheMonitor()
        val waiting = await(s.runtime, jobId, "waiting")
        assertEquals("network", waiting.getString("wait_reason"))
        assertEquals(0, waiting.getInt("retry_count"))
        Thread.sleep(3_000)
        assertEquals(RunState.Stage.ACTIVE, app.offlineRun.state.value.stage)
        assertEquals(RunPhase.WAITING_NETWORK, app.offlineRun.state.value.summary?.phase)
        s.runtime.ok("network_set", JSONObject().put("kind", "wifi"), mutation = true)
        await(s.runtime, jobId, "completed")
        awaitComponentGone()
        assertTrue("no resumed range: ${mediaRequests()}", resumedWithRange())
        assertOnePublishedCopy(s.runtime)
    }

    @Test
    fun aUserInitiatedJobStoppedByLostConnectivityIsRestartedByTheSystemWhenItReturns() {
        assumeTrue("user-initiated jobs exist from API 34", Build.VERSION.SDK_INT >= 34)
        val s = start()
        val generation = app.offlineRun.state.value.generation
        try {
            shellOutput("svc wifi disable")
            shellOutput("svc data disable")
            waitUntil("system stop", 60_000) { app.offlineRun.state.value.stage != RunState.Stage.ACTIVE }
            ControlledEnvironment.releaseStall()
            awaitStage(RunState.Stage.IDLE, 60_000)
            assertTrue("job not kept for a restart", PlatformLauncher(app).isActive())
            Thread.sleep(3_000)
            assertEquals(generation, app.offlineRun.state.value.generation)
        } finally {
            shellOutput("svc wifi enable")
            shellOutput("svc data enable")
        }
        waitUntil("restart", 120_000) { app.offlineRun.state.value.generation > generation }
        await(s.runtime, s.jobIds.single(), "completed")
        awaitComponentGone()
        assertTrue("no resumed range: ${mediaRequests()}", resumedWithRange())
        assertOnePublishedCopy(s.runtime)
    }

    @Test
    fun theRestrictedStandbyBucketLeavesAConsistentState() {
        val s = start()
        try {
            shell("input keyevent KEYCODE_HOME")
            shellOutput("am set-standby-bucket ${app.packageName} restricted")
            Thread.sleep(3_000)
            ControlledEnvironment.releaseStall()
            awaitComponentGone(120_000)
        } finally {
            shellOutput("am set-standby-bucket ${app.packageName} active")
        }
        val after = job(s.runtime, s.jobIds.single())
        assertEquals("stopped", executorState(s.runtime))
        when (after.getString("state")) {
            "completed" -> assertOnePublishedCopy(s.runtime)
            "queued" -> assertTrue(after.getBoolean("has_partial"))
            else -> throw AssertionError("unexpected state after a restricted bucket: $after")
        }
    }

    @Test
    fun aDeniedNotificationPermissionDoesNotStopTheDownload() {
        assumeTrue("runtime notification permission exists from API 33", Build.VERSION.SDK_INT >= 33)
        assumeTrue(
            "POST_NOTIFICATIONS is granted; revoke it before the run (pm revoke kills the process)",
            app.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED,
        )
        val s = start(plainScenario("pad_bytes" to PADDED_BYTES - VIDEO_BYTES), held = false)
        await(s.runtime, s.jobIds.single(), "completed")
        awaitComponentGone()
        assertOnePublishedCopy(s.runtime)
    }

    @Test
    fun belowApi30NothingIsLaunched() {
        assumeTrue("offline downloads need API 30; this device has ${Build.VERSION.SDK_INT}", Build.VERSION.SDK_INT < 30)
        assertEquals(RequestResult.Unsupported, requestControlled())
        assertEquals(false, PlatformLauncher(app).isActive())
    }

    private companion object {
        /** The first test after a cold boot also waits for the engine's first start. */
        const val HELD_TIMEOUT_MS = 60_000L
    }
}
