package pl.altani.spike.controlled

import android.content.Context
import android.os.Build
import com.chaquo.python.Python
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeEndpoint
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.bridge.ChaquopyEndpoint
import pl.altani.spike.offline.AppStorage
import pl.altani.spike.offline.ExecutorControl
import pl.altani.spike.offline.ExecutorStart

/** Debug builds only: a runtime on the controlled server, with its own database and library, apart from the real one. */
object ControlledEnvironment {
    const val BASE = "http://127.0.0.1:8765"

    enum class Storage { FS, CONTENT_URI }

    fun directory(context: Context, name: String): File = File(context.filesDir, "controlled-content/$name")

    /** [wrap] holds or watches single calls into Python; [apiLevel] stands in for the device's, [storage] null omits it. */
    fun runtime(
        context: Context,
        name: String,
        storage: Storage? = Storage.FS,
        base: String = BASE,
        apiLevel: Int = Build.VERSION.SDK_INT,
        wrap: (BridgeEndpoint) -> BridgeEndpoint = { it },
    ): BridgeRuntime {
        val dir = directory(context, name)
        var storageArg: JSONObject? = null
        return BridgeRuntime(
            endpointFactory = {
                val bridge = Python.getInstance().getModule("android_host").callAttr("create_bridge", base)
                val (authority, entry) = when (storage) {
                    // the provider's "controlled" entry covers controlled-content/, so the prefix names the rest
                    Storage.CONTENT_URI -> "${context.packageName}.controlled" to "controlled/$name/library"
                    else -> null to null
                }
                if (storage != null) {
                    storageArg = AppStorage.register(bridge, File(dir, AppStorage.LIBRARY_DIR), authority, entry, apiLevel)
                }
                wrap(ChaquopyEndpoint(bridge))
            },
            engineArgs = {
                JSONObject()
                    .put("cache_dir", File(dir, "cache").absolutePath)
                    .put("history_path", File(dir, "state.db").absolutePath)
                    .put("antibot_delay_sec", 0)
                    .put("access", JSONObject().put("use_flaresolverr", false))
                    // loopback traffic: the device's real network kind would only add the mobile-data consent step
                    .put("network", "wifi")
                    .put("storage", storageArg)
            },
        )
    }

    fun executor(runtime: BridgeRuntime): ExecutorControl =
        ExecutorControl(runtime.scope) { method, args -> runtime.call(method, args, mutation = method != "executor_status") }

    class Listing(val series: JSONObject, val episode: JSONObject)

    /** search → episodes, as the product will do it; [index] counts from the top of the fixture listing. */
    suspend fun listing(runtime: BridgeRuntime, index: Int = 0): Listing {
        val hit = runtime.call("search", JSONObject().put("query", "frieren")).data().getJSONArray("hits").getJSONObject(0)
        val ref = JSONObject()
        listOf("id", "slug", "title", "url").forEach { ref.put(it, hit.get(it)) }
        val listing = runtime.call("episodes", JSONObject().put("series", ref)).data()
        return Listing(listing.getJSONObject("series"), listing.getJSONArray("episodes").getJSONObject(index))
    }

    /** Queues the episode and returns its job id; nothing runs until the executor is started. */
    suspend fun enqueue(runtime: BridgeRuntime, listing: Listing): String {
        val selection = JSONObject().put("mode", "episodes").put("numbers", JSONArray().put(listing.episode.get("number")))
        val preview = JSONObject()
            .put("series", listing.series)
            .put("episodes", JSONArray().put(listing.episode))
            .put("selection", selection)
        val plan = runtime.call("queue_preview", preview).data().getJSONObject("plan")
        val added = runtime.call("queue_enqueue", JSONObject().put("plan", plan), mutation = true).data().getJSONArray("added")
        return added.getJSONObject(0).getString("job_id")
    }

    suspend fun job(runtime: BridgeRuntime, jobId: String): JSONObject =
        runtime.call("queue_job", JSONObject().put("job_id", jobId)).data().getJSONObject("job")

    /** Polls `queue_job` until the job is in one of [states]; `failed` or `cancelled` throws unless asked for. */
    suspend fun awaitJob(runtime: BridgeRuntime, jobId: String, vararg states: String, timeoutMs: Long = 60_000): JSONObject =
        withTimeout(timeoutMs) {
            var job = job(runtime, jobId)
            while (job.getString("state") !in states) {
                check(job.getString("state") !in setOf("failed", "cancelled")) { "download ended: $job" }
                delay(200)
                job = job(runtime, jobId)
            }
            job
        }

    /** Queues the episode, runs the executor and waits for the offline copy `playback_prepare` then prefers. */
    suspend fun download(runtime: BridgeRuntime, listing: Listing, timeoutMs: Long = 60_000): String {
        val jobId = enqueue(runtime, listing)
        when (val started = executor(runtime).start()) {
            is ExecutorStart.Started, ExecutorStart.AlreadyRunning -> Unit
            else -> error("executor did not start: $started")
        }
        awaitJob(runtime, jobId, "completed", timeoutMs = timeoutMs)
        return jobId
    }

    fun BridgeResult.data(): JSONObject =
        when (this) {
            is BridgeResult.Ok -> data
            is BridgeResult.Failure -> error("bridge call failed: $code $params")
            BridgeResult.NotStarted -> error("bridge call not started")
        }

    /** `POST /_control/scenario`; resets the server's request log. */
    fun configure(scenario: JSONObject, base: String = BASE): JSONObject = request("$base/_control/scenario", scenario)

    /** `POST /_control/release`: a response held by the scenario's `stall_files` goes on. */
    fun releaseStall(base: String = BASE): JSONObject = request("$base/_control/release", JSONObject())

    /** `POST /_control/expire`: every URL resolved so far answers 403 `expired`; the next resolve gets working ones. */
    fun expireUrls(base: String = BASE): JSONObject = request("$base/_control/expire", JSONObject())

    /** `GET /_control/log`: the scenario, the resolve count and every media request with its status. */
    fun serverLog(base: String = BASE): JSONObject = request("$base/_control/log", null)

    private fun request(url: String, body: JSONObject?): JSONObject {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 5_000
            connection.readTimeout = 5_000
            if (body != null) {
                connection.requestMethod = "POST"
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val code = connection.responseCode
            check(code == 200) { "controlled server answered $code" }
            return JSONObject(connection.inputStream.bufferedReader().readText())
        } finally {
            connection.disconnect()
        }
    }
}
