package pl.altani.spike.bridge

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** Follows the device's network for the engine; `create_engine` reads [kind], later changes go out as `network_set`. */
class NetworkSync(
    initial: String,
    scope: CoroutineScope,
    private val engineRequested: () -> Boolean,
    private val send: suspend (kind: String) -> BridgeResult,
    private val log: (String) -> Unit = {},
) {
    private val _kind = MutableStateFlow(initial)
    val kind: StateFlow<String> = _kind.asStateFlow()

    // what the engine was told last, by its creation arguments or by network_set
    private var known = initial

    init {
        // a StateFlow is conflated, so a burst of changes is sent as its last kind, one call at a time
        scope.launch {
            _kind.collect { next ->
                if (next == known) return@collect
                if (!engineRequested()) {
                    known = next
                    return@collect
                }
                when (val reply = send(next)) {
                    is BridgeResult.Ok -> {
                        known = next
                        log("network_set $next")
                    }
                    is BridgeResult.Failure -> log("network_set $next failed: ${reply.code}")
                    BridgeResult.NotStarted -> log("network_set $next not started")
                }
            }
        }
    }

    /** Any thread. */
    fun update(kind: String) {
        _kind.value = kind
    }
}
