package pl.altani.spike.playback

import android.content.Context
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime

/** One player for the life of its ViewModel, so rotation never interrupts it; main thread only. */
@UnstableApi
class PlaybackController(
    private val context: Context,
    private val bridge: BridgeRuntime,
    private val uiScope: CoroutineScope,
    private val log: (String) -> Unit,
    private val periodicMs: Long = 10_000,
    val gate: TransferGate = TransferGate(),
    private val hold: TransferHold = BlockingHold(gate),
    // false only for the T9 trial, which drives the gate itself and measures Media3 without the controller's help
    private val manageGate: Boolean = true,
    stuckBufferingTimeoutMs: Int? = null,
) {
    enum class Phase { IDLE, PREPARING, PLAYING, PAUSED, BUFFERING, WAITING_CONSENT, REFRESHING, ENDED, STOPPED }

    data class State(
        val phase: Phase = Phase.IDLE,
        val mode: String? = null,
        val variant: String? = null,
        val refreshes: Int = 0,
        val variantChanges: Int = 0,
        val error: String? = null,
        val pauses: Int = 0,
        val consentRequired: Boolean = false,
    )

    data class Rewind(val fromMs: Long, val toMs: Long)

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()

    private val _writes = MutableStateFlow<List<Pair<PositionReporter.Write, BridgeResult>>>(emptyList())

    /** Every position write and its reply, in the order the bridge answered them. */
    val writes: StateFlow<List<Pair<PositionReporter.Write, BridgeResult>>> = _writes.asStateFlow()

    var lastRewind: Rewind? = null
        private set

    val player: ExoPlayer = ExoPlayer.Builder(context)
        .setLoadControl(
            // the rotation rewind is served from memory, so playback does not stall for it
            hold.loadControl(DefaultLoadControl.Builder().setBackBuffer(BACK_BUFFER_MS, true).build()),
        )
        .apply { stuckBufferingTimeoutMs?.let { setStuckBufferingDetectionTimeoutMs(it) } }
        .build()
        .also { it.addListener(Listener()) }

    private var session: JSONObject? = null
    private var source: PlaybackSource? = null
    private var reporter: PositionReporter? = null
    private var refreshGate = RefreshGate()
    private var ticker: Job? = null
    private var internalSeek = false
    private var consented = false

    // paused by the controller once the buffer ran out behind a closed gate, so the gate's opening resumes it
    private var heldByGate = false
    private var pendingRefresh: Pair<PlaybackSource.Online, Long>? = null

    init {
        if (manageGate) {
            uiScope.launch { bridge.events.events.collect(::onEvent) }
            // a resync may have skipped the consent notice; the network kind is enough to set the gate again
            uiScope.launch { bridge.events.resync.collect { syncNetwork() } }
            bridge.events.start()
        }
    }

    fun start(series: JSONObject, episode: JSONObject, criteria: JSONObject? = null): Job =
        uiScope.launch {
            stopSession()
            _state.value = State(phase = Phase.PREPARING)
            val begin = bridge.call("playback_begin", JSONObject().put("series", series).put("episode", episode), mutation = true)
            if (begin !is BridgeResult.Ok) return@launch stop(begin)
            val playbackSession = begin.data.getJSONObject("playback_session")
            session = playbackSession
            reporter = PositionReporter(bridge.scope, playbackSession, { method, args ->
                bridge.call(method, args, mutation = true)
            }) { write, result -> _writes.update { it + (write to result) } }
            val args = JSONObject().put("playback_session", playbackSession).put("series", series).put("episode", episode)
            criteria?.let { args.put("criteria", it) }
            val prepared = bridge.call("playback_prepare", args)
            if (prepared !is BridgeResult.Ok) return@launch stop(prepared)
            val chosen = PlaybackSource.parse(prepared.data)
            _state.update { it.copy(mode = if (chosen is PlaybackSource.Local) chosen.kind else "online") }
            play(chosen, prepared.data.getJSONObject("resume").optLong("position_ms"))
        }

    /** After an Activity recreation: playback went on; step back so the viewer sees the last seconds again. */
    fun onViewAttached(recreated: Boolean) {
        if (!recreated || !player.playWhenReady || source == null) return
        val from = player.currentPosition
        val to = (from - ROTATION_REWIND_MS).coerceAtLeast(0)
        reporter?.rotationFloorMs = from
        lastRewind = Rewind(from, to)
        seekInternally(to)
        log("rotation rewind ${from}ms -> ${to}ms")
    }

    /** Home, another app or the screen lock; never a configuration change. Coming back does not resume. */
    fun onBackground() {
        heldByGate = false
        if (player.playWhenReady) player.pause()
    }

    fun resume() {
        heldByGate = false
        if (source != null) player.play()
    }

    /** Mobile data for this viewing only; the bridge ends it with the viewing. */
    fun allowCellular(): Job =
        uiScope.launch {
            val playbackSession = session ?: return@launch
            val result = bridge.call("playback_allow_cellular", JSONObject().put("playback_session", playbackSession), mutation = true)
            if (result is BridgeResult.Ok && session === playbackSession) {
                consented = true
                openGate()
            } else if (result is BridgeResult.Failure) {
                log("mobile data not allowed: ${result.code}")
            }
        }

    fun seekTo(positionMs: Long) = player.seekTo(positionMs)

    fun close() {
        stopSession()
        _state.value = State()
    }

    fun release() {
        stopSession()
        player.release()
    }

    private fun play(chosen: PlaybackSource, positionMs: Long) {
        source = chosen
        if (chosen is PlaybackSource.Online) {
            _state.update { it.copy(variant = chosen.variant.label) }
        }
        player.setMediaSource(MediaSources.create(context, chosen, hold))
        seekInternally(positionMs)
        player.prepare()
        player.playWhenReady = true
    }

    private fun seekInternally(positionMs: Long) {
        internalSeek = true
        try {
            player.seekTo(positionMs)
        } finally {
            internalSeek = false
        }
    }

    private fun refresh(online: PlaybackSource.Online, positionMs: Long) {
        val playbackSession = session ?: return
        if (!gate.isOpen) {
            // the contract allows no refresh without consent; the refresh gate stays busy until it runs
            pendingRefresh = online to positionMs
            _state.update { it.copy(phase = Phase.WAITING_CONSENT) }
            return
        }
        _state.update { it.copy(phase = Phase.REFRESHING, refreshes = it.refreshes + 1) }
        uiScope.launch {
            val args = JSONObject().put("session_id", online.sessionId).put("playback_session", playbackSession)
            val result = bridge.call("stream_refresh", args)
            refreshGate.finished()
            if (result !is BridgeResult.Ok) return@launch stop(result)
            val fresh = PlaybackSource.parse(result.data) as PlaybackSource.Online
            if (fresh.variantChanged) {
                // the bridge only offers variants that still meet the criteria, so a change is not a failure
                _state.update { it.copy(variantChanges = it.variantChanges + 1) }
                log("variant changed to ${fresh.variant.label}")
            }
            play(fresh, positionMs)
        }
    }

    private fun stop(failure: BridgeResult) {
        val code = (failure as? BridgeResult.Failure)?.code ?: "not_started"
        stop(code)
    }

    private fun stop(code: String) {
        player.stop()
        ticker?.cancel()
        _state.update { it.copy(phase = Phase.STOPPED, error = code) }
        log("playback stopped: $code")
    }

    private fun onEvent(event: JSONObject) {
        val data = event.optJSONObject("data") ?: return
        when (event.optString("type")) {
            "playback.consent_required" -> {
                val id = data.optJSONObject("playback_session")?.optString("id")
                if (id != null && id == session?.optString("id") && !consented) closeGate()
            }
            "network.changed" -> applyNetwork(data.optString("kind"))
        }
    }

    private suspend fun syncNetwork() {
        val reply = bridge.call("network_get")
        if (reply is BridgeResult.Ok) applyNetwork(reply.data.optString("kind"))
    }

    private fun applyNetwork(kind: String) {
        when (kind) {
            "wifi" -> openGate()
            "cellular" -> if (!consented) closeGate()
            "offline" -> closeGate()
        }
    }

    private fun closeGate() {
        if (session == null || !gate.isOpen) return
        gate.close()
        _state.update { it.copy(consentRequired = true) }
        log("player transfers held")
        if (player.playbackState == Player.STATE_BUFFERING) holdForConsent()
    }

    private fun openGate() {
        if (gate.isOpen) return
        gate.open()
        _state.update { it.copy(consentRequired = false) }
        log("player transfers resumed")
        if (heldByGate) {
            heldByGate = false
            player.play()
        }
        pendingRefresh?.let { (online, position) ->
            pendingRefresh = null
            refresh(online, position)
        }
    }

    /** Media3 fails a player that buffers without progress for long; pausing it while waiting for consent avoids that. */
    private fun holdForConsent() {
        if (!manageGate || gate.isOpen || !player.playWhenReady || source !is PlaybackSource.Online) return
        heldByGate = true
        player.pause()
    }

    private fun stopSession() {
        ticker?.cancel()
        reporter?.let { current ->
            if (source != null) current.explicit(player.currentPosition, duration())
            current.close()
        }
        reporter = null
        session = null
        source = null
        consented = false
        heldByGate = false
        pendingRefresh = null
        if (manageGate) {
            gate.open()
            _state.update { it.copy(consentRequired = false) }
        }
        refreshGate = RefreshGate()
        lastRewind = null
        player.stop()
        player.clearMediaItems()
    }

    private fun duration(): Long? = player.duration.takeIf { it > 0 }

    private fun startTicker() {
        ticker?.cancel()
        ticker = uiScope.launch {
            while (isActive) {
                delay(periodicMs)
                reporter?.periodic(player.currentPosition, duration())
            }
        }
    }

    private inner class Listener : Player.Listener {
        override fun onIsPlayingChanged(isPlaying: Boolean) {
            if (isPlaying) {
                _state.update { it.copy(phase = Phase.PLAYING) }
                startTicker()
            } else {
                ticker?.cancel()
            }
        }

        override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
            if (!playWhenReady && reason == Player.PLAY_WHEN_READY_CHANGE_REASON_USER_REQUEST && source != null) {
                if (heldByGate) {
                    _state.update { it.copy(phase = Phase.WAITING_CONSENT) }
                } else {
                    _state.update { it.copy(phase = Phase.PAUSED, pauses = it.pauses + 1) }
                }
                reporter?.explicit(player.currentPosition, duration())
            }
        }

        override fun onPlaybackStateChanged(playbackState: Int) {
            when (playbackState) {
                Player.STATE_BUFFERING -> {
                    if (state.value.phase == Phase.PLAYING) _state.update { it.copy(phase = Phase.BUFFERING) }
                    holdForConsent()
                }
                Player.STATE_ENDED -> {
                    val end = duration() ?: player.currentPosition
                    reporter?.ended(end, end)
                    _state.update { it.copy(phase = Phase.ENDED) }
                }
            }
        }

        override fun onPositionDiscontinuity(oldPosition: Player.PositionInfo, newPosition: Player.PositionInfo, reason: Int) {
            if (reason == Player.DISCONTINUITY_REASON_SEEK && !internalSeek && source != null) {
                reporter?.explicit(newPosition.positionMs, duration())
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            val online = source as? PlaybackSource.Online
            val code = expiredUrlCode(error)
            val position = player.currentPosition
            if (online == null || code == null) {
                reporter?.explicit(position, duration())
                stop("player:${error.errorCodeName}")
                return
            }
            when (refreshGate.request()) {
                RefreshGate.Decision.START -> {
                    log("url expired (http $code) at ${position}ms, refreshing")
                    reporter?.explicit(position, duration())
                    refresh(online, position)
                }
                RefreshGate.Decision.BUSY -> Unit
                RefreshGate.Decision.LIMIT -> stop("refresh_limit")
            }
        }
    }

    companion object {
        const val ROTATION_REWIND_MS = 1_500L
        const val BACK_BUFFER_MS = 5_000
    }
}
