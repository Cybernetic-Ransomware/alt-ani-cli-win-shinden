package pl.altani.spike.bridge

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONObject

/** Events are invalidation signals only; after each new head [resync] tells holders to re-read their snapshots. */
class EventsPump(
    private val source: suspend (method: String, args: JSONObject) -> BridgeResult,
    private val scope: CoroutineScope,
    private val waitMs: Int = 25_000,
    private val retryDelayMs: Long = 2_000,
) {
    data class State(
        val running: Boolean = false,
        val epoch: String? = null,
        val nextSeq: Long = 0,
        val received: Long = 0,
        val heads: Int = 0,
        val lastError: String? = null,
    )

    private val _state = MutableStateFlow(State())
    private val _events = MutableSharedFlow<JSONObject>(extraBufferCapacity = 256, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    private val _resync = MutableSharedFlow<Unit>(extraBufferCapacity = 1, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    private val lock = Any()
    private var job: Job? = null

    val state: StateFlow<State> = _state.asStateFlow()
    val events: SharedFlow<JSONObject> = _events.asSharedFlow()
    val resync: SharedFlow<Unit> = _resync.asSharedFlow()

    fun start() {
        synchronized(lock) {
            if (job?.isActive == true) return
            job = scope.launch { run() }
        }
    }

    suspend fun stop() {
        val running = synchronized(lock) { job.also { job = null } }
        running?.cancelAndJoin()
    }

    private suspend fun run() {
        _state.update { it.copy(running = true) }
        try {
            while (true) {
                val head = source("events_head", JSONObject())
                if (head !is BridgeResult.Ok) {
                    if (!retry(head)) return
                    continue
                }
                val epoch = head.data.getString("epoch")
                var seq = head.data.getLong("seq")
                _state.update { it.copy(epoch = epoch, nextSeq = seq, heads = it.heads + 1, lastError = null) }
                _resync.tryEmit(Unit)
                while (true) {
                    val cursor = JSONObject().put("epoch", epoch).put("seq", seq)
                    val reply = source("events_next", JSONObject().put("cursor", cursor).put("max", 100).put("wait_ms", waitMs))
                    if (reply !is BridgeResult.Ok) {
                        if (!retry(reply)) return
                        break
                    }
                    val data = reply.data
                    if (data.getBoolean("resync") || data.getString("epoch") != epoch) break
                    val batch = data.getJSONArray("events")
                    for (i in 0 until batch.length()) {
                        _events.tryEmit(batch.getJSONObject(i))
                    }
                    seq = data.getLong("next_seq")
                    _state.update { it.copy(nextSeq = seq, received = it.received + batch.length()) }
                }
            }
        } finally {
            _state.update { it.copy(running = false) }
        }
    }

    /** False when retrying cannot help: the call was cancelled, the engine is gone or the request is wrong. */
    private suspend fun retry(failure: BridgeResult): Boolean {
        val code = (failure as? BridgeResult.Failure)?.code ?: "not_started"
        _state.update { it.copy(lastError = code) }
        if (failure.isCancelled || (failure is BridgeResult.Failure && failure.kind in TERMINAL_KINDS)) return false
        delay(retryDelayMs)
        return true
    }

    private companion object {
        val TERMINAL_KINDS = setOf("closed", "not_found", "invalid_input")
    }
}
