package pl.altani.spike.playback

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import androidx.media3.exoplayer.LoadControl
import androidx.media3.exoplayer.analytics.PlayerId
import androidx.media3.exoplayer.source.TrackGroupArray
import androidx.media3.exoplayer.trackselection.ExoTrackSelection
import androidx.media3.exoplayer.upstream.Allocator
import androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** K1: the LoadControl refuses to load while the gate is closed. */
@UnstableApi
class GatedLoadControl(private val base: LoadControl, private val gate: TransferGate) : LoadControl {
    override fun onPrepared(playerId: PlayerId) = base.onPrepared(playerId)

    override fun onTracksSelected(
        parameters: LoadControl.Parameters,
        trackGroups: TrackGroupArray,
        trackSelections: Array<out ExoTrackSelection?>,
    ) = base.onTracksSelected(parameters, trackGroups, trackSelections)

    override fun onStopped(playerId: PlayerId) = base.onStopped(playerId)

    override fun onReleased(playerId: PlayerId) = base.onReleased(playerId)

    override fun getAllocator(playerId: PlayerId): Allocator = base.getAllocator(playerId)

    override fun getBackBufferDurationUs(playerId: PlayerId): Long = base.getBackBufferDurationUs(playerId)

    override fun retainBackBufferFromKeyframe(playerId: PlayerId): Boolean = base.retainBackBufferFromKeyframe(playerId)

    override fun shouldStartPlayback(parameters: LoadControl.Parameters): Boolean = base.shouldStartPlayback(parameters)

    override fun shouldContinueLoading(parameters: LoadControl.Parameters): Boolean =
        gate.isOpen && base.shouldContinueLoading(parameters)
}

class GateClosedException : IOException("transfer gate closed")

/** K2: `open()` fails while the gate is closed; [GateRetryPolicy] keeps retrying until it opens. */
@UnstableApi
class ThrowingGateDataSource(upstream: DataSource, private val gate: TransferGate) : ForwardingDataSource(upstream) {
    override fun open(dataSpec: DataSpec): Long {
        if (!gate.isOpen) throw GateClosedException()
        return upstream.open(dataSpec)
    }
}

@UnstableApi
class GateRetryPolicy(private val base: LoadErrorHandlingPolicy, private val pollMs: Long = 500) : LoadErrorHandlingPolicy {
    private fun closedGate(error: Throwable?): Boolean = generateSequence(error) { it.cause }.any { it is GateClosedException }

    override fun getFallbackSelectionFor(
        fallbackOptions: LoadErrorHandlingPolicy.FallbackOptions,
        loadErrorInfo: LoadErrorHandlingPolicy.LoadErrorInfo,
    ): LoadErrorHandlingPolicy.FallbackSelection? =
        if (closedGate(loadErrorInfo.exception)) null else base.getFallbackSelectionFor(fallbackOptions, loadErrorInfo)

    // errorCount also counts the gate's failures, so real errors are cut off by it rather than by the loader
    override fun getRetryDelayMsFor(loadErrorInfo: LoadErrorHandlingPolicy.LoadErrorInfo): Long =
        when {
            closedGate(loadErrorInfo.exception) -> pollMs
            loadErrorInfo.errorCount > REAL_ERROR_RETRIES -> C.TIME_UNSET
            else -> base.getRetryDelayMsFor(loadErrorInfo)
        }

    override fun getMinimumLoadableRetryCount(dataType: Int): Int = Int.MAX_VALUE

    private companion object {
        const val REAL_ERROR_RETRIES = 3
    }
}

/** Numbers each `open()` as `X-Probe-Seq`; [outer] stamps the gate before the candidate's check, [inner] before sending. */
@UnstableApi
class TransferProbe(private val gate: TransferGate) {
    enum class Point { BEFORE_CHECK, AFTER_CHECK }

    class Open(val seq: Int, val file: String, val start: TransferGate.Stamp) {
        @Volatile var send: TransferGate.Stamp? = null

        @Volatile var outcome: String? = null

        /** Began after a close and went out before the gate opened again. */
        val violation: Boolean
            get() = send.let { it != null && !start.open && !it.open && it.closures == start.closures }

        /** Passed the check while open, but the gate closed before the request went out. */
        val raced: Boolean
            get() = send.let { it != null && start.open && !it.open }

        override fun toString() = "#$seq $file start=${start.open} send=${send?.open} $outcome"
    }

    class Pause(val point: Point, val file: Regex) {
        val reached = CountDownLatch(1)
        val release = CountDownLatch(1)
        val used = AtomicBoolean()

        @Volatile var open: Open? = null
    }

    private val counter = AtomicInteger()
    private val pauses = CopyOnWriteArrayList<Pause>()
    val opens = ConcurrentHashMap<Int, Open>()

    /** Called on the loading thread with the response code, or null on success; may close the gate. */
    @Volatile var onResult: ((Open, Int?) -> Unit)? = null

    fun pauseAt(point: Point, file: Regex): Pause = Pause(point, file).also { pauses += it }

    fun all(): List<Open> = opens.values.sortedBy { it.seq }

    private fun pause(point: Point, file: String, open: Open?) {
        val pause = pauses.firstOrNull { it.point == point && it.file.matches(file) && !it.used.get() } ?: return
        if (!pause.used.compareAndSet(false, true)) return
        pause.open = open
        pause.reached.countDown()
        check(pause.release.await(30, TimeUnit.SECONDS)) { "probe pause at $point never released" }
    }

    fun outer(next: DataSource.Factory): DataSource.Factory =
        DataSource.Factory {
            object : ForwardingDataSource(next.createDataSource()) {
                override fun open(dataSpec: DataSpec): Long {
                    val file = fileOf(dataSpec.uri)
                    pause(Point.BEFORE_CHECK, file, null)
                    val record = Open(counter.incrementAndGet(), file, gate.stamp())
                    opens[record.seq] = record
                    try {
                        return upstream.open(dataSpec.withAdditionalHeaders(mapOf(HEADER to record.seq.toString())))
                    } catch (e: IOException) {
                        if (record.outcome == null) record.outcome = e.javaClass.simpleName
                        throw e
                    }
                }
            }
        }

    fun inner(next: DataSource.Factory): DataSource.Factory =
        DataSource.Factory {
            object : ForwardingDataSource(next.createDataSource()) {
                override fun open(dataSpec: DataSpec): Long {
                    val record = checkNotNull(opens[dataSpec.httpRequestHeaders[HEADER]?.toInt()]) { "unprobed open" }
                    pause(Point.AFTER_CHECK, record.file, record)
                    record.send = gate.stamp()
                    try {
                        val length = upstream.open(dataSpec)
                        record.outcome = "ok"
                        onResult?.invoke(record, null)
                        return length
                    } catch (e: HttpDataSource.InvalidResponseCodeException) {
                        record.outcome = "http ${e.responseCode}"
                        onResult?.invoke(record, e.responseCode)
                        throw e
                    }
                }
            }
        }

    companion object {
        const val HEADER = "X-Probe-Seq"

        /** The path after `/m/<exp>/<sig>/`, as the server logs it. */
        fun fileOf(uri: Uri): String = uri.pathSegments.drop(3).joinToString("/")
    }
}

enum class Candidate { K1, K2, K3, K1_K3 }

@UnstableApi
fun Candidate.hold(gate: TransferGate, probe: TransferProbe): TransferHold {
    val candidate = this
    return object : TransferHold {
        override fun loadControl(base: LoadControl): LoadControl =
            if (candidate == Candidate.K1 || candidate == Candidate.K1_K3) GatedLoadControl(base, gate) else base

        override fun upstream(http: DataSource.Factory): DataSource.Factory {
            val sent = probe.inner(http)
            val held = when (candidate) {
                Candidate.K1 -> sent
                Candidate.K2 -> DataSource.Factory { ThrowingGateDataSource(sent.createDataSource(), gate) }
                Candidate.K3, Candidate.K1_K3 -> DataSource.Factory { GatedDataSource(sent.createDataSource(), gate) }
            }
            return probe.outer(held)
        }

        override fun errorPolicy(base: LoadErrorHandlingPolicy): LoadErrorHandlingPolicy =
            if (candidate == Candidate.K2) GateRetryPolicy(base) else base
    }
}
