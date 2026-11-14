package pl.altani.spike.controlled

import android.content.Context
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

/** Debug builds only: a runtime on the controlled server, with its own database and library, apart from the real one. */
object ControlledEnvironment {
    const val BASE = "http://127.0.0.1:8765"

    enum class Storage { FS, CONTENT_URI }

    /** [wrap] lets a test hold or watch single calls on their way into Python. */
    fun runtime(
        context: Context,
        name: String,
        storage: Storage = Storage.FS,
        base: String = BASE,
        wrap: (BridgeEndpoint) -> BridgeEndpoint = { it },
    ): BridgeRuntime {
        val dir = File(context.filesDir, "controlled-content/$name")
        var storageArg: JSONObject? = null
        return BridgeRuntime(
            endpointFactory = {
                val py = Python.getInstance()
                val bridge = py.getModule("android_host").callAttr("create_bridge", base)
                val libraryRoot = File(dir, "library").absolutePath
                // a platform store: the core fs store publishes by hard link, which app storage refuses
                val (authority, entry) = when (storage) {
                    Storage.FS -> null to null
                    // the provider's "controlled" entry covers controlled-content/, so the prefix names the rest
                    Storage.CONTENT_URI -> "${context.packageName}.controlled" to "controlled/$name/library"
                }
                storageArg = JSONObject(
                    py.getModule("controlled_host").callAttr("storage", bridge, libraryRoot, authority, entry).toString(),
                )
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

    class Listing(val series: JSONObject, val episode: JSONObject)

    /** search → episodes, as the product will do it; [index] counts from the top of the fixture listing. */
    suspend fun listing(runtime: BridgeRuntime, index: Int = 0): Listing {
        val hit = runtime.call("search", JSONObject().put("query", "frieren")).data().getJSONArray("hits").getJSONObject(0)
        val ref = JSONObject()
        listOf("id", "slug", "title", "url").forEach { ref.put(it, hit.get(it)) }
        val listing = runtime.call("episodes", JSONObject().put("series", ref)).data()
        return Listing(listing.getJSONObject("series"), listing.getJSONArray("episodes").getJSONObject(index))
    }

    /** Queues the episode, runs the executor and waits for the offline copy `playback_prepare` then prefers. */
    suspend fun download(runtime: BridgeRuntime, listing: Listing, timeoutMs: Long = 60_000) {
        val selection = JSONObject().put("mode", "episodes").put("numbers", JSONArray().put(listing.episode.get("number")))
        val preview = JSONObject()
            .put("series", listing.series)
            .put("episodes", JSONArray().put(listing.episode))
            .put("selection", selection)
        val plan = runtime.call("queue_preview", preview).data().getJSONObject("plan")
        val added = runtime.call("queue_enqueue", JSONObject().put("plan", plan), mutation = true).data().getJSONArray("added")
        val jobId = added.getJSONObject(0).getString("job_id")
        runtime.call("executor_start", JSONObject(), mutation = true).data()
        withTimeout(timeoutMs) {
            while (true) {
                val job = runtime.call("queue_job", JSONObject().put("job_id", jobId)).data().getJSONObject("job")
                when (val state = job.getString("state")) {
                    "completed" -> return@withTimeout
                    "failed", "cancelled" -> error("download ended $state")
                }
                delay(200)
            }
        }
    }

    private fun BridgeResult.data(): JSONObject =
        when (this) {
            is BridgeResult.Ok -> data
            is BridgeResult.Failure -> error("bridge call failed: $code")
            BridgeResult.NotStarted -> error("bridge call not started")
        }

    /** `POST /_control/scenario`; resets the server's request log. */
    fun configure(scenario: JSONObject, base: String = BASE): JSONObject = request("$base/_control/scenario", scenario)

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
