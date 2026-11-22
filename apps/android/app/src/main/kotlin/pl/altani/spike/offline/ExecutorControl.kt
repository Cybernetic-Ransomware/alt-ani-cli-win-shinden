package pl.altani.spike.offline

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.async
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult

sealed interface ExecutorStart {
    class Started(val recovery: JSONObject) : ExecutorStart

    data object AlreadyRunning : ExecutorStart

    /** Another owner (process or bridge) executes this database's queue. */
    data object Busy : ExecutorStart

    /** An earlier stop returned `ended: false`; its I/O still runs and ownership is kept until it ends. */
    data object Closing : ExecutorStart

    /** The file store cannot be used, e.g. `storage.no_atomic_publish` below API 30. */
    data class Unsupported(val code: String) : ExecutorStart

    data class Failed(val failure: BridgeResult) : ExecutorStart
}

sealed interface ExecutorStop {
    data object Ended : ExecutorStop

    /** `ended: false`: the executor is `closing`, a later [ExecutorControl.stop] waits again. */
    data class StillStopping(val stuckJobs: List<String>) : ExecutorStop

    data class Failed(val failure: BridgeResult) : ExecutorStop
}

sealed interface ExecutorStatus {
    data class Known(val state: String, val stuckJobs: List<String>, val owner: String) : ExecutorStatus

    data class Failed(val failure: BridgeResult) : ExecutorStatus
}

/** Same-kind calls share one bridge call run in [scope], start and stop never overlap; no loop, timer or retry. */
class ExecutorControl(
    private val scope: CoroutineScope,
    private val call: suspend (method: String, args: JSONObject) -> BridgeResult,
) {
    private enum class Kind { START, STOP }

    private class InFlight(val kind: Kind, val result: Deferred<Any>)

    private val lock = Mutex()
    private var inFlight: InFlight? = null

    suspend fun start(): ExecutorStart = single(Kind.START) { startOnce() } as ExecutorStart

    /** Concurrent stops share the first caller's [timeoutMs]. */
    suspend fun stop(timeoutMs: Long = 10_000): ExecutorStop = single(Kind.STOP) { stopOnce(timeoutMs) } as ExecutorStop

    suspend fun status(): ExecutorStatus =
        when (val result = call("executor_status", JSONObject())) {
            is BridgeResult.Ok -> parsed({ ExecutorStatus.Failed(it) }) {
                ExecutorStatus.Known(
                    state = result.data.getString("state"),
                    stuckJobs = result.data.optJSONArray("stuck_jobs").strings(),
                    owner = result.data.optString("owner", "unknown"),
                )
            }
            else -> ExecutorStatus.Failed(result)
        }

    private suspend fun single(kind: Kind, block: suspend () -> Any): Any {
        while (true) {
            val (deferred, joined) = lock.withLock {
                val current = inFlight
                when {
                    current == null || current.result.isCompleted ->
                        scope.async { block() }.also { inFlight = InFlight(kind, it) } to true
                    current.kind == kind -> current.result to true
                    else -> current.result to false
                }
            }
            if (joined) return deferred.await()
            // the other kind first: a stop sent during a start would race its ownership check
            deferred.join()
        }
    }

    private suspend fun startOnce(): ExecutorStart =
        when (val result = call("executor_start", JSONObject())) {
            is BridgeResult.Ok -> parsed({ ExecutorStart.Failed(it) }) {
                if (result.data.optBoolean("already_running")) {
                    ExecutorStart.AlreadyRunning
                } else {
                    ExecutorStart.Started(result.data.optJSONObject("recovery") ?: JSONObject())
                }
            }
            is BridgeResult.Failure -> when (result.code) {
                "executor.busy" -> ExecutorStart.Busy
                "executor.closing" -> ExecutorStart.Closing
                in UNSUPPORTED -> ExecutorStart.Unsupported(result.code)
                else -> ExecutorStart.Failed(result)
            }
            BridgeResult.NotStarted -> ExecutorStart.Failed(result)
        }

    private suspend fun stopOnce(timeoutMs: Long): ExecutorStop =
        when (val result = call("executor_stop", JSONObject().put("timeout_ms", timeoutMs))) {
            is BridgeResult.Ok -> parsed({ ExecutorStop.Failed(it) }) {
                if (result.data.getBoolean("ended")) {
                    ExecutorStop.Ended
                } else {
                    ExecutorStop.StillStopping(result.data.optJSONArray("stuck_jobs").strings())
                }
            }
            else -> ExecutorStop.Failed(result)
        }

    private inline fun <T> parsed(failed: (BridgeResult) -> T, read: () -> T): T =
        try {
            read()
        } catch (e: JSONException) {
            failed(BridgeResult.internal("malformed_executor_reply"))
        }

    private fun JSONArray?.strings(): List<String> = if (this == null) emptyList() else List(length()) { getString(it) }

    private companion object {
        val UNSUPPORTED = setOf("storage.no_atomic_publish", "storage.unavailable", "storage.not_configured")
    }
}
