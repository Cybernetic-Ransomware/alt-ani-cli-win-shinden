package pl.altani.spike.ui

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.launch
import pl.altani.spike.AltAniApplication

/** Settings on the process's real bridge; it outlives the tab's composition, so the draft survives a tab change and rotation. */
class SettingsViewModel(app: Application) : AndroidViewModel(app) {
    private val bridge = (app as AltAniApplication).bridge

    val flow = SettingsFlow(
        viewModelScope,
        { method, args -> bridge.call(method, args, mutation = method == SettingsFlow.UPDATE) },
        { Log.i(TAG, it) },
    )

    init {
        viewModelScope.launch { bridge.events.events.collect(flow::onEvent) }
        // a state: its current value is the first read, every new head after a gap or another epoch re-reads
        viewModelScope.launch { bridge.events.resync.collect { flow.refresh() } }
        viewModelScope.launch { bridge.tracker.lateResults.collect(flow::onLateResult) }
        bridge.events.start()
    }

    private companion object {
        const val TAG = "AltAniSettings"
    }
}
