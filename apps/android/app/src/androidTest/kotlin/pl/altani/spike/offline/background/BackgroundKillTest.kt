package pl.altani.spike.offline.background

import androidx.media3.common.util.UnstableApi
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import android.os.Process
import java.io.File
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.MainActivity
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.offline.AppStorage
import pl.altani.spike.offline.DeviceOutcomes
import pl.altani.spike.offline.awaitHeld
import pl.altani.spike.offline.heldScenario
import pl.altani.spike.playback.mediaRequests
import pl.altani.spike.playback.requireControlledServer

/** Driven by `tools/android/background_kill.py`, which kills `arm`'s process; a kill inside `finalizing` is the core's job. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class BackgroundKillTest {
    @get:Rule
    val outcomes = DeviceOutcomes()

    private val phase = InstrumentationRegistry.getArguments().getString("backgroundKill")
    private val marker get() = File(app.filesDir, "background-kill-job")
    private val armed get() = File(app.filesDir, "background-kill-armed")

    @Before
    fun setUp() = requireControlledServer()

    /** Uses the app's own controlled runtime: a restart by the system in the next process must find the same database. */
    @Test
    fun arm() {
        assumeTrue("run by tools/android/background_kill.py", phase == "arm")
        ControlledEnvironment.directory(app, "app").deleteRecursively()
        val runtime = app.controlledBridge!!
        ControlledEnvironment.configure(heldScenario())
        val jobId = runBlocking { ControlledEnvironment.enqueue(runtime, ControlledEnvironment.listing(runtime)) }
        marker.writeText(jobId)
        ActivityScenario.launch(MainActivity::class.java)
        assertEquals(RequestResult.Launched, requestControlled())
        awaitStage(RunState.Stage.ACTIVE)
        awaitHeld(File(ControlledEnvironment.directory(app, "app"), AppStorage.LIBRARY_DIR))
        // the script kills exactly this pid; returning would let the instrumentation end the process instead
        armed.writeText(Process.myPid().toString())
        Thread.sleep(ARM_HOLD_MS)
        fail("the process was not killed within ${ARM_HOLD_MS / 1_000} s")
    }

    @Test
    fun verify() {
        assumeTrue("run by tools/android/background_kill.py", phase == "verify")
        val jobId = marker.readText()
        val runtime = app.controlledBridge!!
        ControlledEnvironment.releaseStall()
        ActivityScenario.launch(MainActivity::class.java).use {
            val requested = requestControlled()
            // a user-initiated job may already have been restarted by the system in this process
            assertTrue("request: $requested", requested == RequestResult.Launched || requested == RequestResult.AlreadyActive)
            runBlocking { ControlledEnvironment.awaitJob(runtime, jobId, "completed", timeoutMs = 90_000) }
            awaitComponentGone()
        }
        // held before the first 5 s checkpoint, so with no resume evidence the engine must not append to the partial
        val gets = mediaRequests().filter { it.getString("file") == "video.mp4" && it.getString("method") == "GET" }
        assertTrue("no transfer after the kill: $gets", gets.size >= 2)
        assertTrue("appended without a checkpoint: $gets", gets.all { it.isNull("range") })
        assertOnePublishedCopy(runtime)
        marker.delete()
        armed.delete()
    }

    private companion object {
        const val ARM_HOLD_MS = 120_000L
    }
}
