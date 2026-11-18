package pl.altani.spike.ui

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.chaquo.python.Python
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.EventsPump

/** Technical screen: borrows the process's bridge and logs outcomes; it never logs `data` of stream methods. */
class TechViewModel(app: Application) : AndroidViewModel(app) {
    private val bridge = (app as AltAniApplication).bridge
    private val _log = MutableStateFlow<List<String>>(emptyList())

    val log: StateFlow<List<String>> = _log.asStateFlow()
    val pump: StateFlow<EventsPump.State> = bridge.events.state

    init {
        viewModelScope.launch {
            bridge.tracker.lateResults.collect { late ->
                add("late ${late.method}: ${describe(late.result)}${if (late.needsRefresh) ", re-read the state" else ""}")
            }
        }
        viewModelScope.launch { bridge.events.events.collect { add("event ${it.optLong("seq")} ${it.optString("type")}") } }
        environment()
        bridgeInfo()
    }

    fun environment() = launchStep("environment") {
        withContext(Dispatchers.IO) {
            Python.getInstance().getModule("android_host").callAttr("environment").toString()
        }
    }

    fun bridgeInfo() = launchStep("bridge_info") {
        when (val reply = bridge.call("bridge_info")) {
            is BridgeResult.Ok -> {
                val data = reply.data
                "contract ${data.getString("contract")}, ${data.getJSONArray("methods").length()} methods, " +
                    "cancel_before_start=${data.getJSONObject("capabilities").optBoolean("cancel_before_start")}, " +
                    "engines created ${bridge.engineCreations}"
            }
            else -> describe(reply)
        }
    }

    fun engineStatus() = launchStep("engine_status") {
        when (val reply = bridge.call("engine_status")) {
            is BridgeResult.Ok -> reply.data.toString()
            else -> describe(reply)
        }
    }

    /** T4: one viewing with a position write; the value survives `am force-stop` when read back. */
    fun writeWatch() = launchStep("watch write") {
        val begun = bridge.call("playback_begin", JSONObject().put("series", PROBE_SERIES).put("episode", PROBE_EPISODE), mutation = true)
        if (begun !is BridgeResult.Ok) return@launchStep describe(begun)
        val position = System.currentTimeMillis() % 3_600_000
        val written = bridge.call(
            "playback_position",
            JSONObject()
                .put("playback_session", begun.data.getJSONObject("playback_session"))
                .put("seq", 1)
                .put("position_ms", position)
                .put("duration_ms", 3_600_000),
            mutation = true,
        )
        when (written) {
            is BridgeResult.Ok -> "position_ms=$position accepted=${written.data.getBoolean("accepted")}"
            else -> describe(written)
        }
    }

    fun readWatch() = launchStep("watch read") {
        val reply = bridge.call("watch_get", JSONObject().put("series_id", PROBE_SERIES.getString("id")))
        if (reply !is BridgeResult.Ok) return@launchStep describe(reply)
        val episodes = reply.data.getJSONArray("episodes")
        if (episodes.length() == 0) return@launchStep "no row yet"
        val watch = episodes.getJSONObject(0)
        "position_ms=${watch.opt("position_ms")} revision=${watch.getInt("revision")}"
    }

    fun toggleEvents() {
        viewModelScope.launch {
            if (pump.value.running) bridge.events.stop() else bridge.events.start()
        }
    }

    fun feasibility() = launchStep("feasibility") {
        val app = getApplication<Application>()
        withContext(Dispatchers.IO) {
            Python.getInstance()
                .getModule("feasibility")
                .callAttr("run", app.filesDir.absolutePath, app.cacheDir.absolutePath)
                .toString()
        }
    }

    private fun launchStep(name: String, body: suspend () -> String) {
        viewModelScope.launch {
            val line = try {
                body()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                "failed: ${e.javaClass.simpleName}"
            }
            add("$name: $line")
        }
    }

    private fun add(line: String) {
        Log.i(TAG, line)
        _log.update { (it + line).takeLast(200) }
    }

    private companion object {
        const val TAG = "AltAniTech"

        val PROBE_SERIES: JSONObject =
            JSONObject().put("id", "t4-probe").put("slug", "t4-probe").put("title", "T4 probe").put("url", "https://shinden.pl/series/0-t4-probe")
        val PROBE_EPISODE: JSONObject =
            JSONObject().put("number", 1).put("title", "probe").put("url", "https://shinden.pl/episode/0-t4-probe/view/0")

        fun describe(result: BridgeResult): String =
            when (result) {
                is BridgeResult.Ok -> "ok"
                is BridgeResult.Failure -> "${result.kind}/${result.code}"
                BridgeResult.NotStarted -> "not started"
            }
    }
}
