package pl.altani.spike.playback

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** What Media3 actually did in one attempt; `PLAYING` alone proves nothing, so playback is confirmed by progress and a frame. */
class PlaybackEvidence(private val requiredProgressMs: Long = REQUIRED_PROGRESS_MS) {
    data class Snapshot(
        val attempt: Int = 0,
        val tracksKnown: Boolean = false,
        val hasVideo: Boolean = false,
        val firstFrame: Boolean = false,
        val audioAdvancing: Boolean = false,
        val progressedMs: Long = 0,
        val requiredProgressMs: Long = REQUIRED_PROGRESS_MS,
    ) {
        val confirmed: Boolean
            get() = tracksKnown && progressedMs >= requiredProgressMs && (firstFrame || !hasVideo)
    }

    private val _snapshot = MutableStateFlow(Snapshot(requiredProgressMs = requiredProgressMs))
    val snapshot: StateFlow<Snapshot> = _snapshot.asStateFlow()

    // the position progress is counted from; moved by seeks so a jump never passes for played time
    private var baseMs: Long? = null

    fun reset() {
        baseMs = null
        _snapshot.update { Snapshot(attempt = it.attempt + 1, requiredProgressMs = requiredProgressMs) }
    }

    fun onTracks(hasVideo: Boolean) = _snapshot.update { it.copy(tracksKnown = true, hasVideo = hasVideo) }

    fun onFirstFrame() = _snapshot.update { it.copy(firstFrame = true) }

    fun onAudioAdvancing() = _snapshot.update { it.copy(audioAdvancing = true) }

    fun onPosition(positionMs: Long, playing: Boolean) {
        if (!playing) return
        val base = baseMs ?: positionMs.also { baseMs = it }
        _snapshot.update { it.copy(progressedMs = maxOf(it.progressedMs, positionMs - base)) }
    }

    fun onSeek(positionMs: Long) {
        if (baseMs == null) return
        baseMs = positionMs - _snapshot.value.progressedMs
    }

    companion object {
        const val REQUIRED_PROGRESS_MS = 3_000L
    }
}
