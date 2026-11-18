package pl.altani.spike.playback

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult

/** One consumer in the runtime's [scope] keeps `seq` order and lands writes after the screen is gone; no retries. */
class PositionReporter(
    scope: CoroutineScope,
    private val session: JSONObject,
    private val call: suspend (method: String, args: JSONObject) -> BridgeResult,
    private val onWritten: (Write, BridgeResult) -> Unit = { _, _ -> },
) {
    data class Write(val method: String, val seq: Long, val positionMs: Long, val durationMs: Long?)

    private val queue = Channel<Write>(Channel.UNLIMITED)
    private var seq = 0L
    private var closed = false

    /** Set after the rotation rewind: periodic writes below it would move the saved position back. */
    @Volatile
    var rotationFloorMs: Long? = null

    private val worker: Job = scope.launch {
        for (write in queue) {
            onWritten(write, call(write.method, args(write)))
        }
    }

    /** The 10 s tick while playing; false when skipped below [rotationFloorMs]. */
    fun periodic(positionMs: Long, durationMs: Long?): Boolean {
        val floor = rotationFloorMs
        if (floor != null) {
            if (positionMs < floor) return false
            rotationFloorMs = null
        }
        return enqueue(POSITION, positionMs, durationMs)
    }

    /** Pause, a manual seek, leaving for the background or an error: always the real position. */
    fun explicit(positionMs: Long, durationMs: Long?): Boolean = enqueue(POSITION, positionMs, durationMs)

    fun ended(positionMs: Long, durationMs: Long): Boolean = enqueue(ENDED, positionMs, durationMs)

    /** Pending writes go first, then `playback_close`; nothing is accepted afterwards. */
    fun close() {
        synchronized(this) {
            if (closed) return
            closed = true
            queue.trySend(Write(CLOSE, 0, 0, null))
            queue.close()
        }
    }

    suspend fun join() = worker.join()

    private fun enqueue(method: String, positionMs: Long, durationMs: Long?): Boolean =
        synchronized(this) {
            if (closed) return false
            seq += 1
            queue.trySend(Write(method, seq, positionMs.coerceAtLeast(0), durationMs?.takeIf { it > 0 })).isSuccess
        }

    private fun args(write: Write): JSONObject {
        val args = JSONObject().put("playback_session", session)
        if (write.method == CLOSE) return args
        args.put("seq", write.seq).put("position_ms", write.positionMs)
        write.durationMs?.let { args.put("duration_ms", it) }
        return args
    }

    companion object {
        const val POSITION = "playback_position"
        const val ENDED = "playback_ended"
        const val CLOSE = "playback_close"
    }
}
