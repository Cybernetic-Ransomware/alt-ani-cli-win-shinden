package pl.altani.spike.offline

import android.os.Build
import android.os.SystemClock
import android.util.Log
import androidx.test.core.app.ApplicationProvider
import java.io.File
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.AssumptionViolatedException
import org.junit.rules.TestWatcher
import org.junit.runner.Description
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.controlled.ControlledEnvironment.data
import pl.altani.spike.playback.mediaRequests
import pl.altani.spike.playback.scenario

/** Padded to 1 MiB and held at 300 kB: yt-dlp's doubling reads have then written past Python's 128 KiB file buffer. */
const val VIDEO_BYTES = 166_754L
const val PADDED_BYTES = 1024L * 1024L
const val STALL_AT = 300_000

/** A download held by the server after [STALL_AT] bytes; yt-dlp's first GET of a direct URL is answered in full. */
fun heldScenario(vararg extra: Pair<String, Any?>): JSONObject =
    scenario(
        "progressive",
        "etag" to true,
        "stall_files" to JSONObject().put("video.mp4", STALL_AT),
        "stall_skip" to 1,
        "pad_bytes" to PADDED_BYTES - VIDEO_BYTES,
        *extra,
    )

fun plainScenario(vararg extra: Pair<String, Any?>): JSONObject = scenario("progressive", "etag" to true, *extra)

/** Logs each outcome (tag `OfflineOutcome`, `files/offline-outcomes.jsonl`); [notExecutable] is never a pass. */
class DeviceOutcomes : TestWatcher() {
    override fun succeeded(description: Description) = record(description, "PASS", null)

    override fun failed(e: Throwable, description: Description) = record(description, "FAIL", e.toString())

    override fun skipped(e: AssumptionViolatedException, description: Description) {
        val message = e.message.orEmpty()
        if (message.startsWith(NOT_EXECUTABLE)) {
            record(description, "NOT_EXECUTABLE", message.removePrefix(NOT_EXECUTABLE))
        } else {
            record(description, "SKIPPED", message)
        }
    }

    private fun record(description: Description, outcome: String, detail: String?) {
        val line = JSONObject()
            .put("test", "${description.testClass.simpleName}.${description.methodName}")
            .put("outcome", outcome)
            .put("detail", detail ?: JSONObject.NULL)
            .put("device", "${Build.MANUFACTURER} ${Build.MODEL} API ${Build.VERSION.SDK_INT}")
        Log.i(TAG, line.toString())
        val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
        synchronized(DeviceOutcomes::class.java) { File(app.filesDir, FILE).appendText("$line\n") }
    }

    companion object {
        const val TAG = "OfflineOutcome"
        const val FILE = "offline-outcomes.jsonl"
        private const val NOT_EXECUTABLE = "not_executable: "

        fun notExecutable(reason: String): Nothing = throw AssumptionViolatedException(NOT_EXECUTABLE + reason)
    }
}

/** Controlled runtimes made by one test, shut down and deleted afterwards. */
class Runtimes {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val open = mutableListOf<BridgeRuntime>()
    private val names = mutableListOf<String>()

    fun newName(): String = "off-${UUID.randomUUID()}".also { names += it }

    fun open(
        name: String,
        storage: ControlledEnvironment.Storage? = ControlledEnvironment.Storage.FS,
        apiLevel: Int = Build.VERSION.SDK_INT,
    ): BridgeRuntime = ControlledEnvironment.runtime(app, name, storage, apiLevel = apiLevel).also { open += it }

    fun library(name: String): File = File(ControlledEnvironment.directory(app, name), AppStorage.LIBRARY_DIR)

    /** `shutdown` must end: a runtime still closing would keep the database's executor lock. */
    fun close(runtime: BridgeRuntime) {
        open.remove(runtime)
        check(runBlocking { runtime.shutdown(15_000) }.ended) { "runtime did not end" }
    }

    fun closeAll() {
        runCatching { ControlledEnvironment.releaseStall() }
        open.toList().forEach { close(it) }
        names.forEach { ControlledEnvironment.directory(app, it).deleteRecursively() }
    }
}

fun BridgeRuntime.ok(method: String, args: JSONObject = JSONObject(), mutation: Boolean = false): JSONObject =
    runBlocking { call(method, args, mutation) }.data()

fun BridgeRuntime.failure(method: String, args: JSONObject = JSONObject()): BridgeResult.Failure =
    runBlocking { call(method, args, mutation = true) } as? BridgeResult.Failure ?: error("$method did not fail")

fun JSONArray.objects(): List<JSONObject> = List(length()) { getJSONObject(it) }

fun waitUntil(what: String, timeoutMs: Long = 20_000, condition: () -> Boolean) {
    val deadline = SystemClock.elapsedRealtime() + timeoutMs
    while (!condition()) {
        check(SystemClock.elapsedRealtime() < deadline) { "$what not reached in ${timeoutMs}ms" }
        Thread.sleep(100)
    }
}

fun partials(library: File): List<File> = File(library, ".work").walkTopDown().filter { it.isFile && it.name.endsWith(".part") }.toList()

/** The download request is held and yt-dlp has written its first bytes to the partial. */
fun awaitHeld(library: File, timeoutMs: Long = 20_000) =
    waitUntil("a held transfer", timeoutMs) {
        mediaRequests().any { it.optBoolean("stalled") } && partials(library).any { it.length() > 0 }
    }

/** A user action reaches the running attempt at the scheduler's next monitor pass (every 0.25 s); release after it. */
fun releaseAfterTheMonitor() {
    Thread.sleep(1_000)
    ControlledEnvironment.releaseStall()
}

fun workEntries(library: File): List<String> {
    val work = File(library, ".work")
    return work.walkTopDown().drop(1).map { it.relativeTo(work).invariantSeparatorsPath }.sorted().toList()
}
