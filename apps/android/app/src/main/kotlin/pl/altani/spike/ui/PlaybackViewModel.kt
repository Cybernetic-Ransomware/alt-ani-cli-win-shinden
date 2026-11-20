package pl.altani.spike.ui

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.media3.common.C
import androidx.media3.common.Player
import androidx.media3.common.Tracks
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.analytics.AnalyticsListener
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import pl.altani.spike.AltAniApplication
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.playback.PlaybackArbiter
import pl.altani.spike.playback.PlaybackController
import pl.altani.spike.playback.PlaybackEvidence
import pl.altani.spike.playback.WatchSaved

/** Holds the players across Activity recreation; the runtimes belong to the Application, not to this. */
@UnstableApi
class PlaybackViewModel(app: Application) : AndroidViewModel(app) {
    enum class Mode { CONTROLLED, CATALOG }

    private val application = app as AltAniApplication
    private val runtime = application.controlledBridge
    private val _log = MutableStateFlow<List<String>>(emptyList())

    val log: StateFlow<List<String>> = _log.asStateFlow()
    val controller: PlaybackController? = runtime?.let { PlaybackController(app, it, viewModelScope, ::add) }

    private val _catalogController = MutableStateFlow<PlaybackController?>(null)

    /** Created on the first catalogue pick, on the real bridge; in a release build the only player. */
    val catalogController: StateFlow<PlaybackController?> = _catalogController.asStateFlow()

    val evidence = PlaybackEvidence()

    private val _catalogSaved = MutableSharedFlow<WatchSaved>(extraBufferCapacity = 64)

    /** Watch writes the engine accepted for catalogue viewings, so the catalogue knows when to read its history again. */
    val catalogSaved: SharedFlow<WatchSaved> = _catalogSaved.asSharedFlow()

    private val arbiter = PlaybackArbiter<Mode>(viewModelScope) { previous ->
        // closing bumps the controller's generation, so a begin still on its way is abandoned instead of played
        when (previous) {
            Mode.CONTROLLED -> controller?.close()
            Mode.CATALOG -> _catalogController.value?.close()
        }
    }
    val mode: StateFlow<Mode?> = arbiter.mode

    private var lastPick: PlaybackPick? = null

    /** Configures the server, then search → episodes → `playback_begin` → `playback_prepare` → Media3. */
    fun playControlled(scenario: JSONObject, criteria: JSONObject? = null, local: Boolean = false, episodeIndex: Int = 0): Job =
        onMain {
            arbiter.launch(Mode.CONTROLLED) { ticket ->
                val runtime = runtime ?: return@launch add("controlled playback needs a debug build")
                val player = controller ?: return@launch
                try {
                    withContext(Dispatchers.IO) { ControlledEnvironment.configure(scenario) }
                    val listing = ControlledEnvironment.listing(runtime, episodeIndex)
                    if (local) {
                        add("downloading episode ${listing.episode.opt("number")}")
                        ControlledEnvironment.download(runtime, listing)
                    }
                    if (!ticket.current) return@launch
                    player.start(listing.series, listing.episode, criteria).join()
                    add("playing ${scenario.optString("kind")}: ${player.state.value}")
                } catch (e: CancellationException) {
                    throw e
                } catch (e: IllegalStateException) {
                    // raised by the controlled environment itself, with a bridge code and nothing else
                    add("controlled flow failed: ${e.message}")
                } catch (e: Exception) {
                    // the class only: a transport message may quote a URL
                    add("controlled flow failed: ${e.javaClass.simpleName}")
                }
            }.join()
        }

    fun playCatalog(pick: PlaybackPick): Job =
        onMain {
            lastPick = pick
            arbiter.launch(Mode.CATALOG) { ticket ->
                val player = catalogPlayer()
                // the previous media goes first, so none of its frames or positions count for this attempt
                player.close()
                evidence.reset()
                if (!ticket.current) return@launch
                val source = if (pick.onlineId == null) "auto" else "chosen"
                add("catalog start episode ${pick.episode.label} source=$source from_start=${pick.fromStart}")
                player.start(
                    pick.series.toJson(),
                    pick.episode.toJson(),
                    sessionId = pick.sessionId,
                    onlineId = pick.onlineId,
                    fromStart = pick.fromStart,
                ).join()
            }.join()
        }

    /** A start-over that already played has saved its new position, so the retry resumes from there. */
    fun retryCatalog() {
        val pick = lastPick ?: return
        playCatalog(if (_catalogController.value?.playedThisViewing == true) pick.copy(fromStart = false) else pick)
    }

    fun onViewAttached(recreated: Boolean) {
        controller?.onViewAttached(recreated)
        _catalogController.value?.onViewAttached(recreated)
    }

    fun onBackground() {
        controller?.onBackground()
        _catalogController.value?.onBackground()
    }

    private fun catalogPlayer(): PlaybackController =
        _catalogController.value ?: PlaybackController(
            application,
            application.bridge,
            viewModelScope,
            ::add,
            deviceNetwork = application.networkSync.kind,
        ).also {
            it.player.addAnalyticsListener(EvidenceListener())
            sampleProgress(it)
            viewModelScope.launch { it.saved.collect(_catalogSaved::emit) }
            _catalogController.value = it
        }

    private fun sampleProgress(player: PlaybackController) {
        viewModelScope.launch {
            var confirmedAttempt = 0
            while (isActive) {
                delay(SAMPLE_MS)
                if (mode.value != Mode.CATALOG) continue
                evidence.onPosition(player.player.currentPosition, player.player.isPlaying)
                val seen = evidence.snapshot.value
                if (seen.confirmed && seen.attempt != confirmedAttempt) {
                    confirmedAttempt = seen.attempt
                    add("media3 confirmed: progressed_ms=${seen.progressedMs} video=${seen.hasVideo} first_frame=${seen.firstFrame}")
                }
            }
        }
    }

    private inner class EvidenceListener : AnalyticsListener {
        override fun onTracksChanged(eventTime: AnalyticsListener.EventTime, tracks: Tracks) {
            if (!tracks.isEmpty) evidence.onTracks(hasVideo = tracks.isTypeSelected(C.TRACK_TYPE_VIDEO))
        }

        override fun onRenderedFirstFrame(eventTime: AnalyticsListener.EventTime, output: Any, renderTimeMs: Long) = evidence.onFirstFrame()

        override fun onAudioPositionAdvancing(eventTime: AnalyticsListener.EventTime, playoutStartSystemTimeMs: Long) =
            evidence.onAudioAdvancing()

        override fun onPositionDiscontinuity(
            eventTime: AnalyticsListener.EventTime,
            oldPosition: Player.PositionInfo,
            newPosition: Player.PositionInfo,
            reason: Int,
        ) {
            if (reason == Player.DISCONTINUITY_REASON_SEEK) evidence.onSeek(newPosition.positionMs)
        }
    }

    // tests call in from the instrumentation thread; the arbiter and the players live on the main thread
    private fun onMain(body: suspend () -> Unit): Job = viewModelScope.launch(Dispatchers.Main.immediate) { body() }

    private fun add(line: String) {
        Log.i(TAG, line)
        _log.update { (it + line).takeLast(50) }
    }

    override fun onCleared() {
        controller?.release()
        _catalogController.value?.release()
    }

    private companion object {
        const val TAG = "AltAniPlayback"
        const val SAMPLE_MS = 500L
    }
}
