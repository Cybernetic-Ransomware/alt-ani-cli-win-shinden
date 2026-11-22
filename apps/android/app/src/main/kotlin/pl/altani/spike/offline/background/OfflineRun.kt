package pl.altani.spike.offline.background

import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max
import kotlin.math.min
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.EventsPump
import pl.altani.spike.offline.ExecutorControl
import pl.altani.spike.offline.ExecutorStart
import pl.altani.spike.offline.ExecutorStop

/** The runtime a session drives; [call] is read-only here, every write goes through [executor]. */
class RunBackend(
    val executor: ExecutorControl,
    val events: EventsPump,
    val call: suspend (method: String, args: JSONObject) -> BridgeResult,
)

sealed interface RequestResult {
    data object Launched : RequestResult

    /** A component is scheduled, running or still finishing; nothing new was launched. */
    data object AlreadyActive : RequestResult

    data object Unsupported : RequestResult

    /** The system refused the launch, e.g. the app was not visible. */
    data object NotAllowed : RequestResult

    data object ReadFailed : RequestResult
}

data class RunState(
    val generation: Long = 0,
    val target: RunTarget? = null,
    val stage: Stage = Stage.IDLE,
    val summary: RunSummary? = null,
    val lastEnd: String? = null,
) {
    enum class Stage { IDLE, STARTING, ACTIVE, DRAINING }
}

/** A session lives as long as its Android component; the engine still chooses what runs, Activities never stop it. */
class OfflineRun(
    private val scope: CoroutineScope,
    private val launcher: RunLauncher,
    private val backends: (RunTarget) -> RunBackend?,
    private val attention: AttentionNotifier,
    /** Monotonic across process deaths within one boot (`elapsedRealtime`), like a launch's `initiated_at`. */
    private val clock: () -> Long,
    /** Wall time, the engine's clock for `not_before`. */
    private val wallClock: () -> Long,
    private val log: (String) -> Unit = {},
) {
    private inner class Session(
        val generation: Long,
        val target: RunTarget,
        val backend: RunBackend,
        val host: RunHost,
        val initiatedAtMs: Long,
        val scheduledScope: NetworkScope,
        @Volatile var after: Job?,
    ) {
        /** Guarded by [lock]: false once the host finished or the system took it away; nothing is called on it then. */
        var hostAttached = true
        val draining = AtomicBoolean(false)
        @Volatile var drainReschedule = false
        @Volatile var desiredScope = scheduledScope
        @Volatile var observer: Job? = null
        val signals = Channel<Unit>(Channel.CONFLATED)
    }

    private val lock = Any()
    private var session: Session? = null
    private var generation = 0L

    /** Guarded by [lock]: the last drain begun, which the next session's executor calls are ordered after. */
    private var lastDrain: Job? = null

    /** Guarded by [lock]: the request in flight, which concurrent callers share. */
    private var requesting: Deferred<RequestResult>? = null
    private val _state = MutableStateFlow(RunState())

    val state: StateFlow<RunState> = _state.asStateFlow()

    /** From the visible UI only: the one path that begins a new session; concurrent callers share one check-read-launch. */
    suspend fun request(target: RunTarget = RunTarget.APP): RequestResult {
        if (launcher.path == null) return RequestResult.Unsupported
        val backend = backends(target) ?: return RequestResult.Unsupported
        val (op, own) = synchronized(lock) {
            requesting?.let { it to false } ?: scope.async(start = CoroutineStart.LAZY) {
                try {
                    requestOnce(target, backend)
                } finally {
                    synchronized(lock) { requesting = null }
                }
            }.also { requesting = it } to true
        }
        val result = op.await()
        return if (!own && result == RequestResult.Launched) RequestResult.AlreadyActive else result
    }

    private suspend fun requestOnce(target: RunTarget, backend: RunBackend): RequestResult {
        if (synchronized(lock) { session?.hostAttached == true } || launcher.isActive()) return RequestResult.AlreadyActive
        val summary = read(backend) ?: return RequestResult.ReadFailed
        val spec = LaunchSpec(target, summary.scope, clock(), summary.remainingBytes)
        return if (launcher.launch(spec)) RequestResult.Launched else RequestResult.NotAllowed
    }

    /** Returns the session's generation, or [REJECTED] when another component holds one; never blocks the caller. */
    fun attach(host: RunHost, target: RunTarget, initiatedAtMs: Long, networkScope: NetworkScope): Long {
        val backend = backends(target)
        val created = synchronized(lock) {
            val current = session
            if (backend == null || (current != null && current.hostAttached)) {
                log("attach rejected: target=$target current=${current?.generation}")
                return REJECTED
            }
            Session(++generation, target, backend, host, initiatedAtMs, networkScope, lastDrain).also { session = it }
        }
        publish(created, RunState.Stage.STARTING)
        // lazy, so a drain that starts at once still finds the observer to cancel
        created.observer = scope.launch(start = CoroutineStart.LAZY) { run(created) }.also { it.start() }
        return created.generation
    }

    /** `onStopJob`: the host is never finished by us after this; returns whether JobScheduler may restart the job. */
    fun onSystemStop(generation: Long, reason: SystemStop): Boolean {
        val s = detach(generation) ?: return false
        val allowed = reason != SystemStop.USER &&
            RunPolicy.withinWindow(s.initiatedAtMs, clock()) &&
            s.scheduledScope.coveredBy(s.desiredScope) &&
            (!s.draining.get() || s.drainReschedule)
        log("system stop gen=$generation reason=$reason reschedule=$allowed")
        beginDrain(s, reschedule = false, reason = null)
        return allowed
    }

    /** The component ended on its own terms (FGS timeout or destroy): same as a system stop, without a restart. */
    fun onHostGone(generation: Long) {
        val s = detach(generation) ?: return
        log("host gone gen=$generation")
        beginDrain(s, reschedule = false, reason = null)
    }

    /** The notification's Stop: jobs go back to `queued` through `executor.shutdown`, no retry spent. */
    fun stopByUser(generation: Long) {
        val s = synchronized(lock) { session?.takeIf { it.generation == generation && it.hostAttached } } ?: return
        log("stop by user gen=$generation")
        beginDrain(s, reschedule = false, reason = null)
    }

    private fun detach(generation: Long): Session? =
        synchronized(lock) {
            session?.takeIf { it.generation == generation && it.hostAttached }?.also { it.hostAttached = false }
        }

    private suspend fun run(s: Session) {
        attention.clear()
        if (!RunPolicy.withinWindow(s.initiatedAtMs, clock())) {
            log("restart after the window, executor not started")
            beginDrain(s, reschedule = false, reason = AttentionReason.WINDOW_EXPIRED, stopExecutor = false)
            return
        }
        awaitPrevious(s)
        s.backend.events.start()
        if (!startExecutor(s.backend)) {
            beginDrain(s, reschedule = false, reason = AttentionReason.START_FAILED, stopExecutor = false)
            return
        }
        publish(s, RunState.Stage.ACTIVE)
        coroutineScope {
            val listeners = listOf(
                launch { s.backend.events.events.collect { if (it.optString("type") in SIGNALS) s.signals.trySend(Unit) } },
                launch { s.backend.events.resync.collect { s.signals.trySend(Unit) } },
            )
            try {
                observe(s)
            } finally {
                listeners.forEach { it.cancel() }
            }
        }
    }

    /** Every decision follows a fresh read, so a lost event or a stopped pump only delays it until the fallback read. */
    private suspend fun observe(s: Session) {
        var phase: RunPhase? = null
        var phaseSince = 0L
        var claimSince: Long? = null
        var failures = 0
        var shown: NotificationModel? = null
        var lastRead = Long.MIN_VALUE / 2
        var recheckAt: Long? = null
        while (true) {
            if (lastRead > Long.MIN_VALUE / 2) {
                awaitSignal(s, recheckAt)
                val gap = MIN_READ_INTERVAL_MS - (clock() - lastRead)
                if (gap > 0) delay(gap)
            }
            lastRead = clock()
            val summary = read(s.backend)
            if (summary == null) {
                if (++failures >= MAX_READ_FAILURES) {
                    beginDrain(s, reschedule = false, reason = AttentionReason.STOPPED)
                    return
                }
                recheckAt = null
                continue
            }
            failures = 0
            val now = clock()
            if (summary.phase != phase) {
                phase = summary.phase
                phaseSince = now
            }
            claimSince = if (summary.activeIo == 0 && summary.readyNow > 0) claimSince ?: now else null
            s.desiredScope = summary.scope
            val rescheduleAllowed = RunPolicy.withinWindow(s.initiatedAtMs, now) && s.scheduledScope.coveredBy(summary.scope)
            setState(s) { it.copy(summary = summary) }
            when (val decision = RunPolicy.decide(summary, phaseSince, claimSince, now, s.host.path, rescheduleAllowed)) {
                is RunDecision.Keep -> {
                    val model = NotificationModels.session(summary)
                    withHost(s) {
                        it.setWorking(decision.wakeLock)
                        if (model != shown) it.show(s.generation, model)
                    }
                    shown = model
                    recheckAt = decision.recheckAtMs
                }
                is RunDecision.End -> {
                    val reason = when {
                        decision.reason == EndReason.NEEDS_USER -> AttentionReason.NEEDS_USER
                        decision.reason == EndReason.NETWORK_WAIT && !decision.reschedule -> AttentionReason.NETWORK_WAIT
                        else -> null
                    }
                    log("session end gen=${s.generation} ${decision.reason} reschedule=${decision.reschedule}")
                    beginDrain(s, decision.reschedule, reason)
                    return
                }
            }
        }
    }

    private suspend fun awaitSignal(s: Session, recheckAt: Long?) {
        val untilRecheck = recheckAt?.let { max(0, it - clock()) } ?: FALLBACK_READ_MS
        withTimeoutOrNull(min(FALLBACK_READ_MS, untilRecheck)) { s.signals.receive() }
        delay(DEBOUNCE_MS)
        while (s.signals.tryReceive().isSuccess) Unit
    }

    private suspend fun read(backend: RunBackend): RunSummary? {
        val snapshot = backend.call("queue_snapshot", JSONObject().put("states", JSONArray(RunSummary.OPEN_STATES)))
        if (snapshot !is BridgeResult.Ok) return null
        val preferences = backend.call("preferences_get", JSONObject())
        if (preferences !is BridgeResult.Ok) return null
        return try {
            RunSummary.of(snapshot.data, preferences.data, wallClock())
        } catch (e: JSONException) {
            null
        }
    }

    private suspend fun startExecutor(backend: RunBackend): Boolean =
        when (val started = backend.executor.start()) {
            is ExecutorStart.Started, ExecutorStart.AlreadyRunning -> true
            // an earlier stop still waits for I/O; ownership stays with it until it ends, then one start
            ExecutorStart.Closing -> stopExecutor(backend) && backend.executor.start().let {
                it is ExecutorStart.Started || it == ExecutorStart.AlreadyRunning
            }
            else -> {
                log("executor start refused: $started")
                false
            }
        }

    /** True once the executor ended; false leaves it `closing` with ownership kept, recovery settles it later. */
    private suspend fun stopExecutor(backend: RunBackend): Boolean {
        repeat(STOP_ATTEMPTS) {
            when (val stopped = backend.executor.stop(STOP_TIMEOUT_MS)) {
                ExecutorStop.Ended -> return true
                is ExecutorStop.StillStopping -> log("executor still stopping: ${stopped.stuckJobs.size} stuck")
                is ExecutorStop.Failed -> return false
            }
        }
        return false
    }

    /** The system may start the next component before the last drain ended; that drain's `executor_stop` comes first. */
    private suspend fun awaitPrevious(s: Session) {
        val previous = s.after ?: return
        if (!previous.isCompleted) log("gen=${s.generation} waits for the previous session's stop")
        previous.join()
        s.after = null
    }

    private fun beginDrain(s: Session, reschedule: Boolean, reason: AttentionReason?, stopExecutor: Boolean = true) {
        if (!s.draining.compareAndSet(false, true)) return
        s.drainReschedule = reschedule
        publish(s, RunState.Stage.DRAINING)
        val observer = s.observer
        val drain = scope.launch(start = CoroutineStart.LAZY) {
            // never joined: the observer may be the caller, and an unfinished read changes nothing
            observer?.cancel()
            withHost(s) { it.setWorking(false) }
            awaitPrevious(s)
            val ended = !stopExecutor || stopExecutor(s.backend)
            val host = synchronized(lock) {
                if (s.hostAttached) {
                    // a detached session's notice could cover the next session's
                    reason?.let(attention::post)
                    s.hostAttached = false
                    s.host
                } else {
                    null
                }
            }
            host?.finish(reschedule)
            synchronized(lock) { if (session === s) session = null }
            val end = "gen=${s.generation} executor_ended=$ended host_finished=${host != null} reschedule=$reschedule"
            log("session closed $end")
            setState(s) { RunState(generation = s.generation, lastEnd = end) }
        }
        synchronized(lock) { lastDrain = drain }
        drain.start()
    }

    /** Under [lock], so a host the system already stopped never gets a late notification or wake lock. */
    private inline fun withHost(s: Session, block: (RunHost) -> Unit) {
        synchronized(lock) { if (s.hostAttached) block(s.host) }
    }

    private fun publish(s: Session, stage: RunState.Stage) =
        setState(s) { current ->
            RunState(
                generation = s.generation,
                target = s.target,
                stage = stage,
                summary = if (current.generation == s.generation) current.summary else null,
            )
        }

    /** Only the newest generation writes, so a late update of an older session never covers the next one. */
    private fun setState(s: Session, change: (RunState) -> RunState) {
        synchronized(lock) { if (s.generation == generation) _state.value = change(_state.value) }
    }

    companion object {
        const val REJECTED = -1L
        const val STOP_TIMEOUT_MS = 10_000L
        const val STOP_ATTEMPTS = 6
        const val FALLBACK_READ_MS = 60_000L
        const val DEBOUNCE_MS = 500L
        const val MIN_READ_INTERVAL_MS = 1_000L
        const val MAX_READ_FAILURES = 5

        private val SIGNALS = setOf("job.changed", "queue.changed", "preferences.changed", "executor.state", "executor.attempt_stuck")
    }
}
