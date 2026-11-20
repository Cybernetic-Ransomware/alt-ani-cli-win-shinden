package pl.altani.spike.bridge

import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import org.json.JSONObject

/** The bridge's cancel semantics (2.1): a running call is signalled, an unknown id is remembered for its call. */
class FakeBridge : BridgeEndpoint {
    private val lock = ReentrantLock()
    private val idle = lock.newCondition()
    private val running = HashMap<String, CountDownLatch>()
    private val early = HashSet<String>()

    val invoked = CopyOnWriteArrayList<String>()
    val payloads = CopyOnWriteArrayList<JSONObject>()
    val cancels = CopyOnWriteArrayList<String>()
    val engines = AtomicInteger()
    val shutdowns = AtomicInteger()

    /** Runs on the calling thread after the call started and before it registers its op_id. */
    @Volatile
    var beforeRegister: (opId: String) -> Unit = {}

    /** The method body; `cancelled` opens when `cancel(op_id)` or `shutdown` reaches the running call. */
    @Volatile
    var handler: (method: String, cancelled: CountDownLatch) -> String = { _, _ -> ok() }

    @Volatile
    var createEngineReply: () -> String = { ok(JSONObject().put("engine_id", "engine-${engines.get() + 1}")) }

    val cancelSeen = CountDownLatch(1)

    override fun createEngine(payloadJson: String): String {
        val reply = createEngineReply()
        if (JSONObject(reply).getBoolean("ok")) engines.incrementAndGet()
        return reply
    }

    override fun call(method: String, payloadJson: String): String {
        val payload = JSONObject(payloadJson)
        payloads += payload
        val opId = payload.getString("op_id")
        beforeRegister(opId)
        val signal = CountDownLatch(1)
        lock.withLock {
            if (early.remove(opId)) return CANCELLED
            running[opId] = signal
        }
        try {
            invoked += method
            return handler(method, signal)
        } finally {
            lock.withLock {
                running.remove(opId)
                idle.signalAll()
            }
        }
    }

    override fun cancel(opId: String): Boolean {
        cancels += opId
        try {
            lock.withLock {
                running[opId]?.let {
                    it.countDown()
                    return true
                }
                early += opId
                return false
            }
        } finally {
            cancelSeen.countDown()
        }
    }

    /** Signals every running call, then reports `ended` only if none is left within the timeout. */
    override fun shutdown(timeoutSec: Double): String {
        shutdowns.incrementAndGet()
        val ended = lock.withLock {
            running.values.forEach { it.countDown() }
            var left = TimeUnit.MILLISECONDS.toNanos((timeoutSec * 1000).toLong())
            while (running.isNotEmpty() && left > 0) {
                left = idle.awaitNanos(left)
            }
            running.isEmpty()
        }
        return ok(JSONObject().put("ended", ended))
    }

    companion object {
        val CANCELLED: String = failure("cancelled", "cancelled")

        fun ok(data: JSONObject = JSONObject()): String = JSONObject().put("ok", true).put("data", data).toString()

        fun failure(code: String, kind: String): String =
            JSONObject()
                .put("ok", false)
                .put("error", JSONObject().put("code", code).put("kind", kind).put("retryable", false).put("message", "").put("params", JSONObject()))
                .toString()

        fun CountDownLatch.awaitOrFail(what: String, seconds: Long = 10) {
            check(await(seconds, TimeUnit.SECONDS)) { "timed out waiting for $what" }
        }
    }
}
