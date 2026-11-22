package pl.altani.spike.offline.background

import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.EventsPump
import pl.altani.spike.offline.ExecutorControl
import pl.altani.spike.ui.FakeCalls

/** A queue the test edits in place; executor replies are queued, `executor_start`, `executor_stop` and `queue_snapshot` can be held. */
private class FakeEngine {
    val methods: MutableList<String> = Collections.synchronizedList(mutableListOf())
    @Volatile var jobs: List<JSONObject> = emptyList()
    var network = "wifi_only"
    var failReads = false
    var holdReads = false
    val reads = Channel<Unit>(Channel.UNLIMITED)
    val starts = ArrayDeque<BridgeResult>()
    @Volatile var holdStarts = false
    val heldStarts = Channel<BridgeResult>(Channel.UNLIMITED)
    @Volatile var onStart: () -> Unit = {}
    @Volatile var holdStops = false
    val stops = Channel<BridgeResult>(Channel.UNLIMITED)
    val events = Channel<String>(Channel.UNLIMITED)
    private var seq = 1L

    val executorCalls get() = synchronized(methods) { methods.filter { it.startsWith("executor_") || it == "runtime_close" } }
    val snapshots get() = synchronized(methods) { methods.count { it == "queue_snapshot" } }

    suspend fun call(method: String, args: JSONObject): BridgeResult {
        methods += method
        return when (method) {
            "executor_start" -> {
                onStart()
                if (holdStarts) heldStarts.receive() else starts.removeFirstOrNull() ?: started
            }
            "executor_stop" -> if (holdStops) stops.receive() else stopped(ended = true)
            "queue_snapshot" -> {
                if (holdReads) reads.receive()
                if (failReads) FakeCalls.failure("internal", "internal") else BridgeResult.Ok(snapshot(*jobs.toTypedArray()))
            }
            "preferences_get" -> BridgeResult.Ok(preferences(network))
            "events_head" -> BridgeResult.Ok(JSONObject().put("epoch", "e").put("seq", seq))
            "events_next" -> {
                val type = events.receive()
                val event = JSONObject().put("seq", seq).put("type", type)
                seq++
                BridgeResult.Ok(
                    JSONObject().put("epoch", "e").put("events", JSONArray().put(event)).put("next_seq", seq).put("resync", false),
                )
            }
            else -> error("unexpected $method")
        }
    }

    companion object {
        val started = BridgeResult.Ok(JSONObject().put("state", "running").put("already_running", false).put("recovery", JSONObject()))

        fun stopped(ended: Boolean, vararg stuck: String) =
            BridgeResult.Ok(
                JSONObject().put("ended", ended).put("state", if (ended) "stopped" else "closing").put("stuck_jobs", JSONArray(stuck.toList())),
            )
    }
}

private class FakeHost(override val path: RunPath) : RunHost {
    val shown = mutableListOf<Pair<Long, NotificationModel>>()
    val working = mutableListOf<Boolean>()
    val finishes = mutableListOf<Boolean>()

    override fun show(generation: Long, model: NotificationModel) {
        shown += generation to model
    }

    override fun setWorking(working: Boolean) {
        this.working += working
    }

    override fun finish(reschedule: Boolean) {
        finishes += reschedule
    }
}

private class FakeLauncher(override val path: RunPath?) : RunLauncher {
    val launches = mutableListOf<LaunchSpec>()
    var active = false
    var allowed = true

    override fun isActive() = active

    override fun launch(spec: LaunchSpec): Boolean {
        if (allowed) launches += spec
        return allowed
    }
}

private class FakeAttention : AttentionNotifier {
    val posted = mutableListOf<AttentionReason>()
    var clears = 0

    override fun post(reason: AttentionReason) {
        posted += reason
    }

    override fun clear() {
        clears++
    }
}

@OptIn(ExperimentalCoroutinesApi::class)
class OfflineRunTest {
    private val engine = FakeEngine()
    private val attention = FakeAttention()

    private inner class Harness(scope: TestScope, path: RunPath?) {
        val launcher = FakeLauncher(path)
        val host = FakeHost(path ?: RunPath.FOREGROUND_SERVICE)

        // one control and one pump per runtime, as in the app: their single-flight covers every session
        private val backend = RunBackend(
            ExecutorControl(scope.backgroundScope, engine::call),
            EventsPump(engine::call, scope.backgroundScope),
            engine::call,
        )
        val run = OfflineRun(
            scope = scope.backgroundScope,
            launcher = launcher,
            backends = { target -> backend.takeIf { target == RunTarget.APP } },
            attention = attention,
            clock = { scope.testScheduler.currentTime },
            wallClock = { 1_800_000_000_000L },
        )

        fun attach(initiatedAt: Long = 0, scope: NetworkScope = NetworkScope.WIFI) = run.attach(host, RunTarget.APP, initiatedAt, scope)
    }

    private fun test(path: RunPath? = RunPath.FOREGROUND_SERVICE, body: suspend TestScope.(Harness) -> Unit) =
        runTest { body(Harness(this, path)) }

    @Test
    fun `a request launches once and a second request or component is refused while one holds the session`() = test { h ->
        engine.jobs = listOf(job("queued", cellular = true))
        assertEquals(RequestResult.Launched, h.run.request())
        assertEquals(NetworkScope.ANY, h.launcher.launches.single().scope)
        h.launcher.active = true
        assertEquals(RequestResult.AlreadyActive, h.run.request())

        val generation = h.attach()
        runCurrent()
        assertEquals(OfflineRun.REJECTED, h.run.attach(FakeHost(RunPath.FOREGROUND_SERVICE), RunTarget.APP, 0, NetworkScope.WIFI))
        h.launcher.active = false
        assertEquals(RequestResult.AlreadyActive, h.run.request())
        assertEquals(listOf("executor_start"), engine.executorCalls)
        assertTrue(generation > 0)
    }

    @Test
    fun `concurrent requests share one read and one launch`() = test { h ->
        engine.jobs = listOf(job("queued"))
        engine.holdReads = true
        val results = List(3) { async { h.run.request() } }
        runCurrent()
        assertEquals(1, engine.snapshots)
        engine.reads.send(Unit)
        val outcomes = results.awaitAll()
        assertEquals(1, h.launcher.launches.size)
        assertEquals(1, outcomes.count { it == RequestResult.Launched })
        assertEquals(2, outcomes.count { it == RequestResult.AlreadyActive })
        assertEquals(1, engine.snapshots)

        h.launcher.active = true
        assertEquals(RequestResult.AlreadyActive, h.run.request())
        assertEquals(1, h.launcher.launches.size)
    }

    @Test
    fun `no request below API 30, for an unknown target, or when the system refuses`() = test(path = null) { h ->
        assertEquals(RequestResult.Unsupported, h.run.request())
        assertEquals(emptyList<String>(), engine.methods)
    }

    @Test
    fun `a refused launch is reported, not retried`() = test { h ->
        h.launcher.allowed = false
        assertEquals(RequestResult.NotAllowed, h.run.request())
        assertEquals(RequestResult.Unsupported, h.run.request(RunTarget.CONTROLLED))
        engine.failReads = true
        h.launcher.allowed = true
        assertEquals(RequestResult.ReadFailed, h.run.request())
        assertEquals(emptyList<LaunchSpec>(), h.launcher.launches)
    }

    @Test
    fun `a closing executor is stopped first and started once`() = test { h ->
        engine.jobs = listOf(job("running", done = 1, total = 10))
        engine.starts += FakeCalls.failure("executor.closing", "conflict")
        h.attach()
        runCurrent()
        assertEquals(listOf("executor_start", "executor_stop", "executor_start"), engine.executorCalls)
        assertEquals(RunState.Stage.ACTIVE, h.run.state.value.stage)
    }

    @Test
    fun `a busy or unsupported executor ends the session without a retry or a stop`() = test { h ->
        engine.jobs = listOf(job("queued"))
        engine.starts += FakeCalls.failure("executor.busy", "conflict")
        h.attach()
        advanceTimeBy(5 * 60_000)
        assertEquals(listOf("executor_start"), engine.executorCalls)
        assertEquals(listOf(false), h.host.finishes)
        assertEquals(listOf(AttentionReason.START_FAILED), attention.posted)

        engine.starts += FakeCalls.failure("storage.no_atomic_publish", "storage")
        h.attach()
        advanceTimeBy(60_000)
        assertEquals(listOf("executor_start", "executor_start"), engine.executorCalls)
        assertEquals(listOf(false, false), h.host.finishes)
    }

    @Test
    fun `a stop whose I-O has not ended keeps the component until the executor ends`() = test { h ->
        engine.holdStops = true
        h.attach()
        runCurrent()
        assertEquals(listOf("executor_start", "executor_stop"), engine.executorCalls)
        engine.stops.send(FakeEngine.stopped(ended = false, "job-1"))
        runCurrent()
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        assertEquals(RunState.Stage.DRAINING, h.run.state.value.stage)
        engine.stops.send(FakeEngine.stopped(ended = true))
        runCurrent()
        assertEquals(listOf(false), h.host.finishes)
        assertFalse("runtime_close" in engine.methods)
        assertEquals(RunState.Stage.IDLE, h.run.state.value.stage)
    }

    @Test
    fun `a stop that never ends lets the component go after its attempts, ownership stays with the engine`() = test { h ->
        engine.holdStops = true
        repeat(OfflineRun.STOP_ATTEMPTS) { engine.stops.send(FakeEngine.stopped(ended = false, "job-1")) }
        h.attach()
        runCurrent()
        assertEquals(OfflineRun.STOP_ATTEMPTS, engine.executorCalls.count { it == "executor_stop" })
        assertEquals(listOf(false), h.host.finishes)
        assertFalse("runtime_close" in engine.methods)
    }

    @Test
    fun `a system stop returns at once and the late drain never finishes the stopped job`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("running", done = 1, total = 10))
        val generation = h.attach()
        runCurrent()
        val shownBefore = h.host.shown.size
        engine.holdStops = true
        assertTrue(h.run.onSystemStop(generation, SystemStop.CONNECTIVITY))
        runCurrent()
        assertEquals(RunState.Stage.DRAINING, h.run.state.value.stage)
        engine.jobs = emptyList()
        engine.events.send("job.changed")
        engine.stops.send(FakeEngine.stopped(ended = true))
        advanceTimeBy(120_000)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        assertEquals(shownBefore, h.host.shown.size)
        assertFalse(h.run.onSystemStop(generation, SystemStop.OTHER))
        h.run.stopByUser(generation)
        assertEquals(1, engine.executorCalls.count { it == "executor_stop" })
    }

    @Test
    fun `a component restarted before the last drain ended starts its executor only after that stop`() =
        test(RunPath.USER_INITIATED_JOB) { h ->
            engine.jobs = listOf(job("running", done = 1, total = 10))
            val first = h.attach()
            runCurrent()
            val shownBefore = h.host.shown.size
            engine.holdStops = true
            assertTrue(h.run.onSystemStop(first, SystemStop.CONNECTIVITY))
            runCurrent()

            val next = FakeHost(RunPath.USER_INITIATED_JOB)
            val second = h.run.attach(next, RunTarget.APP, 0, NetworkScope.WIFI)
            runCurrent()
            assertTrue(second > first)
            assertEquals(listOf("executor_start", "executor_stop"), engine.executorCalls)
            assertEquals(RunState(generation = second, target = RunTarget.APP, stage = RunState.Stage.STARTING), h.run.state.value)

            engine.stops.send(FakeEngine.stopped(ended = false, "job-1"))
            runCurrent()
            assertEquals(listOf("executor_start", "executor_stop", "executor_stop"), engine.executorCalls)
            engine.holdStarts = true
            engine.stops.send(FakeEngine.stopped(ended = true))
            runCurrent()
            assertEquals(listOf("executor_start", "executor_stop", "executor_stop", "executor_start"), engine.executorCalls)
            // the old session closed meanwhile; its IDLE must not cover the new one
            assertEquals(RunState(generation = second, target = RunTarget.APP, stage = RunState.Stage.STARTING), h.run.state.value)
            engine.heldStarts.send(FakeEngine.started)
            advanceTimeBy(5_000)
            assertEquals(second, h.run.state.value.generation)
            assertEquals(RunState.Stage.ACTIVE, h.run.state.value.stage)
            assertEquals(1L, h.run.state.value.summary?.bytesDone)
            assertEquals(emptyList<Boolean>(), h.host.finishes)
            assertEquals(shownBefore, h.host.shown.size)
            assertTrue(next.shown.all { it.first == second } && next.shown.isNotEmpty())
            assertEquals(emptyList<AttentionReason>(), attention.posted)
        }

    /** Real threads: the old drain is held between two stop attempts, the only gap where a new start could slip in. */
    @Test
    fun `a restart cannot start the executor between the last drain's stop attempts`() = runBlocking {
        val pool = Executors.newFixedThreadPool(4)
        val scope = CoroutineScope(SupervisorJob() + pool.asCoroutineDispatcher())
        val startedBetween = CountDownLatch(1)
        try {
            val backend = RunBackend(ExecutorControl(scope, engine::call), EventsPump(engine::call, scope), engine::call)
            val run = OfflineRun(
                scope = scope,
                launcher = FakeLauncher(RunPath.USER_INITIATED_JOB),
                backends = { backend },
                attention = attention,
                clock = { System.nanoTime() / 1_000_000 },
                wallClock = { 1_800_000_000_000L },
                log = { if (it.startsWith("executor still stopping")) startedBetween.await(1, TimeUnit.SECONDS) },
            )
            engine.jobs = listOf(job("running", done = 1, total = 10))
            val first = run.attach(FakeHost(RunPath.USER_INITIATED_JOB), RunTarget.APP, System.nanoTime() / 1_000_000, NetworkScope.WIFI)
            awaitState(run) { it.stage == RunState.Stage.ACTIVE }
            engine.holdStops = true
            engine.onStart = { startedBetween.countDown() }
            assertTrue(run.onSystemStop(first, SystemStop.CONNECTIVITY))
            val second = run.attach(FakeHost(RunPath.USER_INITIATED_JOB), RunTarget.APP, System.nanoTime() / 1_000_000, NetworkScope.WIFI)
            awaitState(run) { engine.executorCalls.size == 2 }
            engine.stops.send(FakeEngine.stopped(ended = false, "job-1"))
            awaitState(run) { engine.executorCalls.size == 3 }
            engine.stops.send(FakeEngine.stopped(ended = true))
            awaitState(run) { it.generation == second && it.stage == RunState.Stage.ACTIVE }
            assertEquals(listOf("executor_start", "executor_stop", "executor_stop", "executor_start"), engine.executorCalls)
        } finally {
            startedBetween.countDown()
            scope.cancel()
            pool.shutdownNow()
        }
    }

    /** Real threads: a restart attaches while the system stop is held at its log line, after the host was detached. */
    @Test
    fun `a restart right after a system stop detached the host waits for that stop`() = runBlocking {
        val pool = Executors.newFixedThreadPool(4)
        val scope = CoroutineScope(SupervisorJob() + pool.asCoroutineDispatcher())
        val inStop = CountDownLatch(1)
        val restarted = CountDownLatch(1)
        try {
            val backend = RunBackend(ExecutorControl(scope, engine::call), EventsPump(engine::call, scope), engine::call)
            val run = OfflineRun(
                scope = scope,
                launcher = FakeLauncher(RunPath.USER_INITIATED_JOB),
                backends = { backend },
                attention = attention,
                clock = { System.nanoTime() / 1_000_000 },
                wallClock = { 1_800_000_000_000L },
                log = {
                    if (it.startsWith("system stop")) {
                        inStop.countDown()
                        restarted.await(1, TimeUnit.SECONDS)
                    }
                },
            )
            engine.jobs = listOf(job("running", done = 1, total = 10))
            val first = run.attach(FakeHost(RunPath.USER_INITIATED_JOB), RunTarget.APP, System.nanoTime() / 1_000_000, NetworkScope.WIFI)
            awaitState(run) { it.stage == RunState.Stage.ACTIVE }
            engine.onStart = { restarted.countDown() }
            val stop = scope.async { run.onSystemStop(first, SystemStop.CONNECTIVITY) }
            assertTrue(inStop.await(5, TimeUnit.SECONDS))
            val second = run.attach(FakeHost(RunPath.USER_INITIATED_JOB), RunTarget.APP, System.nanoTime() / 1_000_000, NetworkScope.WIFI)
            assertTrue(second > first)
            assertTrue(stop.await())
            awaitState(run) { it.generation == second && it.stage == RunState.Stage.ACTIVE }
            assertEquals(listOf("executor_start", "executor_stop", "executor_start"), engine.executorCalls)
        } finally {
            restarted.countDown()
            scope.cancel()
            pool.shutdownNow()
        }
    }

    private suspend fun awaitState(run: OfflineRun, until: (RunState) -> Boolean) =
        withTimeout(10_000) { while (!until(run.state.value)) delay(10) }

    @Test
    fun `a session the system took away while it drained posts no late notice`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("waiting", "source_decision"))
        val generation = h.attach()
        engine.holdStops = true
        advanceTimeBy(RunPolicy.SETTLE_MS + 5_000)
        assertEquals(RunState.Stage.DRAINING, h.run.state.value.stage)
        assertFalse(h.run.onSystemStop(generation, SystemStop.OTHER))
        engine.stops.send(FakeEngine.stopped(ended = true))
        runCurrent()
        assertEquals(emptyList<AttentionReason>(), attention.posted)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        assertEquals(RunState.Stage.IDLE, h.run.state.value.stage)
    }

    @Test
    fun `the user's own stop and an expired window or a narrowed scope forbid a system restart`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("running"))
        assertFalse(h.run.onSystemStop(h.attach(), SystemStop.USER))
        advanceTimeBy(1_000)

        val late = h.attach(initiatedAt = testScheduler.currentTime)
        advanceTimeBy(RunPolicy.RESCHEDULE_WINDOW_MS)
        assertFalse(h.run.onSystemStop(late, SystemStop.CONNECTIVITY))
        advanceTimeBy(1_000)

        val narrowed = h.attach(initiatedAt = testScheduler.currentTime, scope = NetworkScope.ANY)
        advanceTimeBy(2_000)
        assertFalse(h.run.onSystemStop(narrowed, SystemStop.CONNECTIVITY))
        assertEquals(emptyList<Boolean>(), h.host.finishes)
    }

    @Test
    fun `a restart after the window starts nothing and asks the user`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("queued"))
        advanceTimeBy(RunPolicy.RESCHEDULE_WINDOW_MS + 1)
        h.attach(initiatedAt = 0)
        runCurrent()
        assertEquals(emptyList<String>(), engine.executorCalls)
        assertEquals(listOf(false), h.host.finishes)
        assertEquals(listOf(AttentionReason.WINDOW_EXPIRED), attention.posted)
    }

    @Test
    fun `a session that runs past twenty four hours is never cut short by the window`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("running", done = 1, total = 10))
        h.attach(initiatedAt = 0)
        advanceTimeBy(RunPolicy.RESCHEDULE_WINDOW_MS + 60 * 60_000)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        assertEquals(listOf("executor_start"), engine.executorCalls)
    }

    @Test
    fun `the queue running out ends the session even when no event ever arrives`() = test { h ->
        engine.jobs = listOf(job("running", done = 1, total = 10))
        h.attach()
        advanceTimeBy(5_000)
        assertEquals(true, h.host.working.last())
        engine.jobs = listOf(job("completed"))
        advanceTimeBy(OfflineRun.FALLBACK_READ_MS - 10_000)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        advanceTimeBy(15_000)
        assertEquals(listOf(false), h.host.finishes)
        assertEquals(false, h.host.working.last())
        assertEquals(listOf("executor_start", "executor_stop"), engine.executorCalls)
        assertEquals(emptyList<AttentionReason>(), attention.posted)
    }

    @Test
    fun `an event is read promptly and a burst of them at most once a second`() = test { h ->
        engine.jobs = listOf(job("running", done = 0, total = 1_000_000))
        h.attach()
        advanceTimeBy(5_000)
        val before = engine.snapshots
        repeat(20) { i ->
            engine.jobs = listOf(job("running", done = (i + 1) * 10_000L, total = 1_000_000))
            engine.events.send("job.changed")
            advanceTimeBy(100)
        }
        advanceTimeBy(1_000)
        val reads = engine.snapshots - before
        assertTrue("reads=$reads", reads in 1..4)
        assertTrue(h.host.shown.size <= engine.snapshots)
        assertEquals(200_000L, h.run.state.value.summary?.bytesDone)
    }

    @Test
    fun `the notification's stop ends the session, a stale generation is ignored`() = test { h ->
        engine.jobs = listOf(job("running"))
        val generation = h.attach()
        runCurrent()
        h.run.stopByUser(generation + 1)
        runCurrent()
        assertEquals(listOf("executor_start"), engine.executorCalls)
        h.run.stopByUser(generation)
        runCurrent()
        assertEquals(listOf("executor_start", "executor_stop"), engine.executorCalls)
        assertEquals(listOf(false), h.host.finishes)
    }

    @Test
    fun `pausing the last job ends the session at once, cancelling one keeps it for the others`() = test { h ->
        engine.jobs = listOf(job("running"), job("queued"))
        h.attach()
        advanceTimeBy(3_000)
        engine.jobs = listOf(job("running"))
        engine.events.send("job.changed")
        advanceTimeBy(3_000)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        engine.jobs = listOf(job("paused"))
        engine.events.send("job.changed")
        advanceTimeBy(3_000)
        assertEquals(listOf(false), h.host.finishes)
    }

    @Test
    fun `a network wait ends a foreground service after ten minutes and asks the user`() = test { h ->
        engine.jobs = listOf(job("waiting", "network"))
        h.attach()
        advanceTimeBy(RunPolicy.NETWORK_GRACE_MS - 5_000)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        assertFalse(h.host.working.any { it })
        advanceTimeBy(10_000)
        assertEquals(listOf(false), h.host.finishes)
        assertEquals(listOf(AttentionReason.NETWORK_WAIT), attention.posted)
    }

    @Test
    fun `a network wait finishes a user-initiated job for a system restart inside the window`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("waiting", "network"))
        h.attach()
        advanceTimeBy(RunPolicy.NETWORK_GRACE_MS + 5_000)
        assertEquals(listOf(true), h.host.finishes)
        assertEquals(emptyList<AttentionReason>(), attention.posted)
    }

    @Test
    fun `a narrowed scope turns the network wait's restart into a request in the app`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("waiting", "network"))
        h.attach(scope = NetworkScope.ANY)
        advanceTimeBy(RunPolicy.NETWORK_GRACE_MS + 5_000)
        assertEquals(listOf(false), h.host.finishes)
        assertEquals(listOf(AttentionReason.NETWORK_WAIT), attention.posted)
    }

    @Test
    fun `waits for the user end the session after thirty seconds with one notification`() = test(RunPath.USER_INITIATED_JOB) { h ->
        engine.jobs = listOf(job("waiting", "source_decision"), job("failed"))
        h.attach()
        advanceTimeBy(RunPolicy.SETTLE_MS - 5_000)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        advanceTimeBy(10_000)
        assertEquals(listOf(false), h.host.finishes)
        assertEquals(listOf(AttentionReason.NEEDS_USER), attention.posted)
        assertEquals(1, attention.clears)
    }

    @Test
    fun `reads that keep failing end the session instead of holding the component forever`() = test { h ->
        engine.jobs = listOf(job("running"))
        engine.failReads = true
        h.attach()
        advanceTimeBy(OfflineRun.FALLBACK_READ_MS * OfflineRun.MAX_READ_FAILURES)
        assertEquals(listOf(false), h.host.finishes)
        assertEquals(listOf(AttentionReason.STOPPED), attention.posted)
        assertEquals(listOf("executor_start", "executor_stop"), engine.executorCalls)
    }

    @Test
    fun `a host gone on its own drains without being finished again`() = test { h ->
        engine.jobs = listOf(job("running"))
        val generation = h.attach()
        runCurrent()
        h.run.onHostGone(generation)
        runCurrent()
        assertEquals(listOf("executor_start", "executor_stop"), engine.executorCalls)
        assertEquals(emptyList<Boolean>(), h.host.finishes)
        assertEquals(RunState.Stage.IDLE, h.run.state.value.stage)
    }
}
