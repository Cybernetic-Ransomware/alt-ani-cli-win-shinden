package pl.altani.spike.ui

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.launch
import pl.altani.spike.AltAniApplication

/** The catalogue on the process's real bridge; logs carry methods, codes, counts and timings, never URLs or payloads. */
class CatalogViewModel(app: Application) : AndroidViewModel(app) {
    private val bridge = (app as AltAniApplication).bridge

    val flow = CatalogFlow(viewModelScope, { method, args -> bridge.call(method, args) }, { Log.i(TAG, it) })

    init {
        viewModelScope.launch {
            bridge.events.events.collect { event ->
                if (event.optString("type") == "session.closed") {
                    event.optJSONObject("data")?.optString("session_id")?.ifEmpty { null }?.let(flow::onSessionClosed)
                }
            }
        }
        bridge.events.start()
    }

    private companion object {
        const val TAG = "AltAniCatalog"
    }
}
