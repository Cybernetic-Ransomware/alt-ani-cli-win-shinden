package pl.altani.spike.bridge

import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeoutOrNull

/** One bridge call as the client sees it; it stays tracked until Python returns, whatever the caller does. */
class Operation internal constructor(val opId: String, val method: String, val mutation: Boolean) {
    internal enum class Phase { PENDING, RUNNING, SKIPPED, FINISHED }

    internal enum class Delivery { WAITING, READY, ABANDONED }

    internal val phase = AtomicReference(Phase.PENDING)
    internal val delivery = AtomicReference(Delivery.WAITING)

    @Volatile
    internal var result: BridgeResult? = null
}

/** A result no caller received; for a mutation the write may have happened, so re-read the state, never repeat. */
data class LateResult(val opId: String, val method: String, val mutation: Boolean, val result: BridgeResult) {
    val needsRefresh: Boolean
        get() = mutation && !result.isCancelled
}

class OperationTracker {
    private val lock = Any()
    private val operations = HashMap<String, Operation>()
    private val _active = MutableStateFlow(0)
    private val _late = MutableSharedFlow<LateResult>(extraBufferCapacity = 64, onBufferOverflow = BufferOverflow.DROP_OLDEST)

    /** Calls still owned by a worker: queued for the pool or inside Python. */
    val active: StateFlow<Int> = _active.asStateFlow()

    val lateResults: SharedFlow<LateResult> = _late.asSharedFlow()

    fun register(opId: String, method: String, mutation: Boolean): Operation {
        val operation = Operation(opId, method, mutation)
        synchronized(lock) {
            check(operations.putIfAbsent(opId, operation) == null) { "op_id reused" }
            _active.value = operations.size
        }
        return operation
    }

    /** The worker's last check before Python; false once the caller cancelled a queued call. */
    fun start(operation: Operation): Boolean = operation.phase.compareAndSet(Operation.Phase.PENDING, Operation.Phase.RUNNING)

    /** The worker hands over what Python returned (or [BridgeResult.NotStarted]); called exactly once per operation. */
    fun finish(operation: Operation, result: BridgeResult) {
        operation.result = result
        operation.phase.set(Operation.Phase.FINISHED)
        if (!operation.delivery.compareAndSet(Operation.Delivery.WAITING, Operation.Delivery.READY)) {
            publishLate(operation, result)
        }
        forget(operation)
    }

    /** True when Python may already have the call; the bridge's early-cancel memory covers one not registered yet. */
    fun abandon(operation: Operation): Boolean {
        if (operation.phase.compareAndSet(Operation.Phase.PENDING, Operation.Phase.SKIPPED)) {
            operation.delivery.set(Operation.Delivery.ABANDONED)
            forget(operation)
            return false
        }
        if (!operation.delivery.compareAndSet(Operation.Delivery.WAITING, Operation.Delivery.ABANDONED)) {
            // the result was ready, but the caller's cancellation won the race to resume it
            operation.result?.let { publishLate(operation, it) }
            return false
        }
        return operation.phase.get() == Operation.Phase.RUNNING
    }

    suspend fun awaitIdle(timeoutMs: Long): Boolean = withTimeoutOrNull(timeoutMs) { active.first { it == 0 } } != null

    private fun forget(operation: Operation) {
        synchronized(lock) {
            if (operations.remove(operation.opId, operation)) {
                _active.value = operations.size
            }
        }
    }

    private fun publishLate(operation: Operation, result: BridgeResult) {
        if (result !is BridgeResult.NotStarted) {
            _late.tryEmit(LateResult(operation.opId, operation.method, operation.mutation, result))
        }
    }
}
