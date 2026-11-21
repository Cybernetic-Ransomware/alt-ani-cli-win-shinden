package pl.altani.spike.offline

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.ui.FakeCalls

class ExecutorControlTest {
    private val calls = FakeCalls()

    private fun test(body: suspend CoroutineScope.(ExecutorControl) -> Unit) =
        runBlocking { withTimeout(10_000) { body(ExecutorControl(this) { method, args -> calls.call(method, args) }) } }

    private suspend fun settle() = repeat(5) { yield() }

    private fun ok(data: JSONObject) = BridgeResult.Ok(data)

    private fun started(alreadyRunning: Boolean = false) =
        ok(JSONObject().put("state", "running").put("already_running", alreadyRunning).put("recovery", JSONObject().put("orphans", 2)))

    private fun stopped(ended: Boolean, vararg stuck: String) =
        ok(JSONObject().put("ended", ended).put("state", if (ended) "stopped" else "closing").put("stuck_jobs", JSONArray(stuck.toList())))

    private fun methods() = calls.calls.map { it.method }

    @Test
    fun `a start reports its recovery and a second one that the executor already runs`() = test { control ->
        val first = async { control.start() }
        settle()
        calls.last("executor_start").answer(started())
        assertEquals(2, (first.await() as ExecutorStart.Started).recovery.getInt("orphans"))

        val second = async { control.start() }
        settle()
        calls.last("executor_start").answer(started(alreadyRunning = true))
        assertSame(ExecutorStart.AlreadyRunning, second.await())
    }

    @Test
    fun `refusals map to their own outcomes and nothing is retried`() = test { control ->
        val expected = mapOf(
            "executor.busy" to ExecutorStart.Busy,
            "executor.closing" to ExecutorStart.Closing,
            "storage.no_atomic_publish" to ExecutorStart.Unsupported("storage.no_atomic_publish"),
            "storage.unavailable" to ExecutorStart.Unsupported("storage.unavailable"),
            "storage.not_configured" to ExecutorStart.Unsupported("storage.not_configured"),
        )
        for ((code, outcome) in expected) {
            val start = async { control.start() }
            settle()
            calls.last("executor_start").answer(FakeCalls.failure(code, "conflict"))
            assertEquals(outcome, start.await())
        }
        val other = async { control.start() }
        settle()
        val failure = FakeCalls.failure("internal", "internal")
        calls.last("executor_start").answer(failure)
        assertEquals(ExecutorStart.Failed(failure), other.await())
        assertEquals(expected.size + 1, calls.calls.size)
    }

    @Test
    fun `concurrent starts share one bridge call and its result`() = test { control ->
        val starts = List(5) { async { control.start() } }
        settle()
        assertEquals(listOf("executor_start"), methods())
        calls.last("executor_start").answer(started())
        val results = starts.map { it.await() }
        assertTrue(results.all { it === results.first() })
    }

    @Test
    fun `a caller giving up never cancels the start the others wait for`() = test { control ->
        val leaving = async { control.start() }
        settle()
        leaving.cancel()
        val staying = async { control.start() }
        settle()
        assertEquals(1, calls.calls.size)
        assertEquals(false, calls.last("executor_start").cancelled)
        calls.last("executor_start").answer(started())
        assertTrue(staying.await() is ExecutorStart.Started)
    }

    @Test
    fun `a stop waits for a start in flight instead of racing it`() = test { control ->
        val start = async { control.start() }
        settle()
        val stop = async { control.stop() }
        settle()
        assertEquals(listOf("executor_start"), methods())
        calls.last("executor_start").answer(started())
        settle()
        assertEquals(listOf("executor_start", "executor_stop"), methods())
        calls.last("executor_stop").answer(stopped(ended = true))
        assertTrue(start.await() is ExecutorStart.Started)
        assertSame(ExecutorStop.Ended, stop.await())
    }

    @Test
    fun `a stop that has not ended keeps the executor closing until a later stop ends it`() = test { control ->
        val first = async { control.stop(timeoutMs = 100) }
        settle()
        assertEquals(100L, calls.last("executor_stop").args.getLong("timeout_ms"))
        calls.last("executor_stop").answer(stopped(ended = false, "job-1"))
        assertEquals(ExecutorStop.StillStopping(listOf("job-1")), first.await())

        val start = async { control.start() }
        settle()
        calls.last("executor_start").answer(FakeCalls.failure("executor.closing", "conflict"))
        assertSame(ExecutorStart.Closing, start.await())

        val second = async { control.stop() }
        settle()
        assertEquals(10_000L, calls.last("executor_stop").args.getLong("timeout_ms"))
        calls.last("executor_stop").answer(stopped(ended = true))
        assertSame(ExecutorStop.Ended, second.await())
    }

    @Test
    fun `status reads the state, the stuck jobs and the owner`() = test { control ->
        val status = async { control.status() }
        settle()
        calls.last("executor_status").answer(
            ok(JSONObject().put("state", "closing").put("stuck_jobs", JSONArray(listOf("job-2"))).put("owner", "self")),
        )
        assertEquals(ExecutorStatus.Known("closing", listOf("job-2"), "self"), status.await())
    }

    @Test
    fun `a malformed reply is a failure, not a crash`() = test { control ->
        val stop = async { control.stop() }
        settle()
        calls.last("executor_stop").answer(ok(JSONObject().put("state", "stopped")))
        assertEquals("internal", ((stop.await() as ExecutorStop.Failed).failure as BridgeResult.Failure).code)
    }
}
