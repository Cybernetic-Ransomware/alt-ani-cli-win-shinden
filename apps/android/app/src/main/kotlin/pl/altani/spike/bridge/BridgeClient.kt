package pl.altani.spike.bridge

import java.util.UUID
import java.util.concurrent.Executor
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.async
import org.json.JSONObject

/** Cancelling a caller ends only its wait and sends `cancel(op_id)`; the call runs in [scope] until Python returns. */
class BridgeClient(
    private val endpoint: BridgeEndpoint,
    private val scope: CoroutineScope,
    private val callDispatcher: CoroutineDispatcher,
    // never the call pool: a cancel must not queue behind the calls it is meant to stop
    private val cancelExecutor: Executor,
    val tracker: OperationTracker = OperationTracker(),
    private val newOpId: () -> String = { UUID.randomUUID().toString() },
) {
    /** [mutation] marks a call whose late result means "re-read the state" (see [LateResult]). */
    suspend fun call(
        method: String,
        args: JSONObject = JSONObject(),
        mutation: Boolean = false,
        dispatcher: CoroutineDispatcher = callDispatcher,
    ): BridgeResult {
        val operation = tracker.register(newOpId(), method, mutation)
        val payload = JSONObject(args.toString()).put("op_id", operation.opId).toString()
        val work = scope.async(dispatcher) {
            val result = if (tracker.start(operation)) invoke(method, payload) else BridgeResult.NotStarted
            tracker.finish(operation, result)
            result
        }
        try {
            return work.await()
        } catch (e: CancellationException) {
            if (tracker.abandon(operation)) {
                cancelExecutor.execute { runCatching { endpoint.cancel(operation.opId) } }
            }
            throw e
        }
    }

    private fun invoke(method: String, payload: String): BridgeResult =
        try {
            BridgeResult.parse(endpoint.call(method, payload))
        } catch (e: Exception) {
            // a Chaquopy PyException carries Python's message, which may quote request data
            BridgeResult.internal(e.javaClass.simpleName)
        }
}
