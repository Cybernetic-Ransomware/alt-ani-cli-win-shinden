package pl.altani.spike.ui

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.media3.common.util.UnstableApi
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import pl.altani.spike.AltAniApplication
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.playback.PlaybackController

/** Holds the player across Activity recreation; the controlled runtime belongs to the Application, not to this. */
@UnstableApi
class PlaybackViewModel(app: Application) : AndroidViewModel(app) {
    private val runtime = (app as AltAniApplication).controlledBridge
    private val _log = MutableStateFlow<List<String>>(emptyList())

    val log: StateFlow<List<String>> = _log.asStateFlow()
    val controller: PlaybackController? = runtime?.let { PlaybackController(app, it, viewModelScope, ::add) }

    /** Configures the server, then search → episodes → `playback_begin` → `playback_prepare` → Media3. */
    fun playControlled(scenario: JSONObject, criteria: JSONObject? = null, local: Boolean = false): Job =
        viewModelScope.launch {
            val runtime = runtime ?: return@launch add("controlled playback needs a debug build")
            val player = controller ?: return@launch
            try {
                withContext(Dispatchers.IO) { ControlledEnvironment.configure(scenario) }
                val listing = ControlledEnvironment.listing(runtime)
                if (local) {
                    add("downloading episode ${listing.episode.opt("number")}")
                    ControlledEnvironment.download(runtime, listing)
                }
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
        }

    private fun add(line: String) {
        Log.i(TAG, line)
        _log.update { (it + line).takeLast(50) }
    }

    override fun onCleared() {
        controller?.release()
    }

    private companion object {
        const val TAG = "AltAniPlayback"
    }
}
