package pl.altani.spike.offline.background

import android.app.NotificationManager
import android.os.ParcelFileDescriptor
import android.util.Log
import androidx.test.core.app.ApplicationProvider
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.offline.PADDED_BYTES
import pl.altani.spike.offline.objects
import pl.altani.spike.offline.ok
import pl.altani.spike.offline.waitUntil

val app: AltAniApplication get() = ApplicationProvider.getApplicationContext()

/** Called while an Activity of the app is resumed: a launch from the background is refused by the system. */
fun requestControlled(): RequestResult = runBlocking { app.offlineRun.request(RunTarget.CONTROLLED) }

fun awaitStage(stage: RunState.Stage, timeoutMs: Long = 30_000) =
    waitUntil("session $stage", timeoutMs) { app.offlineRun.state.value.stage == stage }

fun awaitComponentGone(timeoutMs: Long = 60_000) =
    waitUntil("component gone", timeoutMs) {
        app.offlineRun.state.value.stage == RunState.Stage.IDLE &&
            !PlatformLauncher(app).isActive() &&
            app.getSystemService(NotificationManager::class.java).activeNotifications
                .none { it.id == DownloadNotifications.NOTIFICATION_RUN }
    }

/** Ends whatever session a failed test left and forgets its runtime, so the next test starts clean. */
fun endSessions() {
    val state = app.offlineRun.state.value
    if (state.stage != RunState.Stage.IDLE) app.offlineRun.stopByUser(state.generation)
    runCatching { ControlledEnvironment.releaseStall() }
    runCatching { awaitComponentGone() }
    app.backgroundControlled = null
}

fun shellOutput(command: String): String =
    ParcelFileDescriptor.AutoCloseInputStream(InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(command))
        .bufferedReader()
        .use { it.readText() }
        .also { Log.i("BackgroundRunTest", "$ $command → ${it.trim()}") }

fun executorState(runtime: BridgeRuntime): String = runtime.ok("executor_status").getString("state")

/** Exactly one published copy of the full size, deep-verified against its recorded SHA-256. */
fun assertOnePublishedCopy(runtime: BridgeRuntime) {
    val items = runtime.ok("library_list").getJSONArray("items").objects()
    assertEquals(1, items.size)
    assertEquals(PADDED_BYTES, items.single().getLong("size_bytes"))
    val verified = runtime.ok("library_verify", JSONObject().put("deep", true), mutation = true).getJSONArray("results")
    assertEquals("ok", verified.getJSONObject(0).getString("result"))
}
