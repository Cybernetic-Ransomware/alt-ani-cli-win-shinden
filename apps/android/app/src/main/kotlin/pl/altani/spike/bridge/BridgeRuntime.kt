package pl.altani.spike.bridge

import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.ThreadFactory
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineName
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** `ended` is true only once the bridge reported every operation returned and no Kotlin worker still holds one. */
data class ShutdownReport(val ended: Boolean, val bridge: BridgeResult, val activeCalls: Int)

/** One per process with one lazily created engine; activities only borrow it, so recreation never adds an engine. */
class BridgeRuntime(
    private val endpointFactory: () -> BridgeEndpoint,
    private val engineArgs: () -> JSONObject,
    callThreads: Int = 4,
) {
    private val callExecutor = Executors.newFixedThreadPool(callThreads, threads("bridge-call"))

    // the long poll has a thread of its own, so it never takes a pool slot from a user action
    private val eventsExecutor = Executors.newSingleThreadExecutor(threads("bridge-events"))
    private val cancelExecutor = Executors.newSingleThreadExecutor(threads("bridge-cancel"))
    private val controlExecutor = Executors.newSingleThreadExecutor(threads("bridge-control"))
    private val callDispatcher = callExecutor.asCoroutineDispatcher()
    private val eventsDispatcher = eventsExecutor.asCoroutineDispatcher()
    private val controlDispatcher = controlExecutor.asCoroutineDispatcher()

    val scope = CoroutineScope(SupervisorJob() + CoroutineName("bridge-runtime"))
    val tracker = OperationTracker()
    val events = EventsPump(source = { method, args -> call(method, args, dispatcher = eventsDispatcher) }, scope = scope)

    private val lock = Any()
    private var endpoint: BridgeEndpoint? = null
    private var setup: Deferred<Setup>? = null
    private val engineCount = AtomicInteger()

    @Volatile
    private var closing = false

    @Volatile
    var released = false
        private set

    /** How many engines this runtime created; stays 1 for the life of the process. */
    val engineCreations: Int
        get() = engineCount.get()

    private class Setup(val client: BridgeClient, val engineId: String)

    private class SetupFailed(val failure: BridgeResult.Failure) : Exception(failure.code)

    suspend fun call(
        method: String,
        args: JSONObject = JSONObject(),
        mutation: Boolean = false,
        dispatcher: CoroutineDispatcher = callDispatcher,
    ): BridgeResult {
        if (closing) return CLOSED
        val ready = try {
            setup().await()
        } catch (e: SetupFailed) {
            return e.failure
        }
        return ready.client.call(method, JSONObject(args.toString()).put("engine_id", ready.engineId), mutation, dispatcher)
    }

    /** A false `ended` keeps threads, Python object and tracker while an operation is still inside Python; repeatable. */
    suspend fun shutdown(timeoutMs: Long): ShutdownReport {
        if (released) return ShutdownReport(true, BridgeResult.Ok(JSONObject().put("ended", true)), 0)
        closing = true
        events.stop()
        val target = synchronized(lock) { endpoint }
        if (target == null) {
            release()
            return ShutdownReport(true, BridgeResult.Ok(JSONObject().put("ended", true)), 0)
        }
        val reply = withContext(controlDispatcher) {
            try {
                BridgeResult.parse(target.shutdown(timeoutMs / 1000.0))
            } catch (e: Exception) {
                BridgeResult.internal(e.javaClass.simpleName)
            }
        }
        val bridgeEnded = reply is BridgeResult.Ok && reply.data.optBoolean("ended")
        val ended = bridgeEnded && tracker.awaitIdle(WORKER_GRACE_MS)
        if (ended) {
            release()
        }
        return ShutdownReport(ended, reply, tracker.active.value)
    }

    private fun setup(): Deferred<Setup> =
        synchronized(lock) {
            setup?.takeUnless { it.isCompleted && it.isCancelled }
                ?: scope.async(callDispatcher) { createEngine() }.also { setup = it }
        }

    private fun createEngine(): Setup {
        val target = synchronized(lock) { endpoint } ?: try {
            endpointFactory().also { synchronized(lock) { endpoint = it } }
        } catch (e: Exception) {
            throw SetupFailed(BridgeResult.internal(e.javaClass.simpleName))
        }
        val reply = try {
            BridgeResult.parse(target.createEngine(engineArgs().toString()))
        } catch (e: Exception) {
            BridgeResult.internal(e.javaClass.simpleName)
        }
        when (reply) {
            is BridgeResult.Ok -> {
                engineCount.incrementAndGet()
                val client = BridgeClient(target, scope, callDispatcher, cancelExecutor, tracker)
                return Setup(client, reply.data.getString("engine_id"))
            }
            is BridgeResult.Failure -> throw SetupFailed(reply)
            BridgeResult.NotStarted -> throw SetupFailed(BridgeResult.internal("not_started"))
        }
    }

    private fun release() {
        released = true
        scope.cancel()
        listOf<ExecutorService>(callExecutor, eventsExecutor, cancelExecutor, controlExecutor).forEach { it.shutdown() }
    }

    companion object {
        const val WORKER_GRACE_MS = 2_000L

        val CLOSED = BridgeResult.Failure("engine_closed", "closed", false, JSONObject())

        private fun threads(name: String): ThreadFactory {
            val count = AtomicInteger()
            return ThreadFactory { task -> Thread(task, "$name-${count.incrementAndGet()}").apply { isDaemon = true } }
        }
    }
}
