package pl.altani.spike.playback

import android.os.SystemClock
import androidx.test.platform.app.InstrumentationRegistry
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import pl.altani.spike.bridge.BridgeEndpoint
import pl.altani.spike.controlled.ControlledEnvironment

/** Skips without the controlled server, or fails when the runner gets `-e controlledServer required`. */
fun requireControlledServer() {
    val reachable = runCatching { ControlledEnvironment.serverLog() }.isSuccess
    if (InstrumentationRegistry.getArguments().getString("controlledServer") == "required") {
        if (!reachable) fail("controlled server not reachable at ${ControlledEnvironment.BASE}")
    } else {
        assumeTrue("controlled server not reachable at ${ControlledEnvironment.BASE}", reachable)
    }
}

fun <T> onMain(block: () -> T): T {
    var result: Result<T>? = null
    InstrumentationRegistry.getInstrumentation().runOnMainSync { result = runCatching(block) }
    return result!!.getOrThrow()
}

/** Polls [read] on the main thread until [done]; a timeout fails with the last value seen. */
fun <T> awaitOnMain(what: String, timeoutMs: Long, read: () -> T, done: (T) -> Boolean): T {
    val deadline = SystemClock.elapsedRealtime() + timeoutMs
    while (true) {
        val value = onMain(read)
        if (done(value)) return value
        if (SystemClock.elapsedRealtime() > deadline) fail("$what not reached in ${timeoutMs}ms, last: $value")
        Thread.sleep(100)
    }
}

fun mediaRequests(): List<JSONObject> {
    val requests: JSONArray = ControlledEnvironment.serverLog().getJSONArray("requests")
    return (0 until requests.length()).map { requests.getJSONObject(it) }
}

fun scenario(kind: String, vararg extra: Pair<String, Any?>): JSONObject =
    JSONObject().put("kind", kind).also { json -> extra.forEach { (k, v) -> json.put(k, v ?: JSONObject.NULL) } }

fun shell(command: String) {
    InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(command).close()
}

/** Holds the next call of one method before Python sees it, and records every call with its reply. */
class CallHold {
    private var method: String? = null

    @Volatile
    private var entered = CountDownLatch(0)

    @Volatile
    private var gate = CountDownLatch(0)

    val calls = CopyOnWriteArrayList<Pair<String, JSONObject>>()
    val replies = CopyOnWriteArrayList<Pair<String, JSONObject>>()

    /** Calls entering and replies leaving, in one order: `call:<method>` and `reply:<method>`, each with its payload. */
    val timeline = CopyOnWriteArrayList<Triple<String, String, JSONObject>>()

    fun hold(name: String) {
        entered = CountDownLatch(1)
        gate = CountDownLatch(1)
        synchronized(this) { method = name }
    }

    fun awaitHeld() = check(entered.await(15, TimeUnit.SECONDS)) { "held call never made" }

    private fun take(name: String): Boolean =
        synchronized(this) {
            (method == name).also { if (it) method = null }
        }

    fun release() = gate.countDown()

    fun wrap(inner: BridgeEndpoint): BridgeEndpoint =
        object : BridgeEndpoint by inner {
            override fun call(method: String, payloadJson: String): String {
                calls += method to JSONObject(payloadJson)
                timeline += Triple("call", method, JSONObject(payloadJson))
                if (take(method)) {
                    val held = gate
                    entered.countDown()
                    check(held.await(30, TimeUnit.SECONDS)) { "$method held too long" }
                }
                return inner.call(method, payloadJson).also {
                    replies += method to JSONObject(it)
                    timeline += Triple("reply", method, JSONObject(it))
                }
            }
        }
}
