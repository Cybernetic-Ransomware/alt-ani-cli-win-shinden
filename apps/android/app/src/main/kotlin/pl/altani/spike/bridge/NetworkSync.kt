package pl.altani.spike.bridge

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/** Follows the device's network for the engine; `create_engine` reads [kind], later changes go out as `network_set`. */
class NetworkSync(
    initial: String,
    scope: CoroutineScope,
    private val engineRequested: () -> Boolean,
    private val send: suspend (kind: String) -> BridgeResult,
    private val log: (String) -> Unit = {},
    private val retryMs: Long = 1_000,
    private val maxRetryMs: Long = 30_000,
    private val callTimeoutMs: Long = 15_000,
) {
    private val _kind = MutableStateFlow(initial)

    /** The device's own view; players hold transfers on it directly, the bridge may still lag behind. */
    val kind: StateFlow<String> = _kind.asStateFlow()

    // what the engine was told last, by its creation arguments or by network_set; null after a call that may or may not have landed
    private var known: String? = initial

    init {
        // a StateFlow is conflated, so a burst of changes is sent as its last kind, one call at a time
        scope.launch { _kind.collect(::deliver) }
    }

    /** Any thread. */
    fun update(kind: String) {
        _kind.value = kind
    }

    /** Until the engine has [next] or the device moves on; a failed call is never left as the engine's last word. */
    private suspend fun deliver(next: String) {
        var wait = retryMs
        while (next != known && _kind.value == next) {
            if (!engineRequested()) {
                known = next
                return
            }
            // a stuck call must not keep a newer kind waiting behind it; the gate already holds on the device's view
            val reply = withTimeoutOrNull(callTimeoutMs) { send(next) }
            if (reply is BridgeResult.Ok) {
                known = next
                log("network_set $next")
                return
            }
            known = null
            val why = when (reply) {
                null -> "timed out"
                is BridgeResult.Failure -> "failed: ${reply.code}"
                else -> "not started"
            }
            log("network_set $next $why, retry in ${wait}ms")
            withTimeoutOrNull(wait) { _kind.first { it != next } }
            wait = (wait * 2).coerceAtMost(maxRetryMs)
        }
    }
}
