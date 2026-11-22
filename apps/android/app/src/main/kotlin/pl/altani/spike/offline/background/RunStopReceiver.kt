package pl.altani.spike.offline.background

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import pl.altani.spike.AltAniApplication

/** No `goAsync`: the stop runs in the coordinator's scope and the running component keeps the process alive. */
class RunStopReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_STOP) return
        val generation = intent.getLongExtra(EXTRA_GENERATION, OfflineRun.REJECTED)
        if (generation == OfflineRun.REJECTED) return
        (context.applicationContext as AltAniApplication).offlineRun.stopByUser(generation)
    }

    companion object {
        const val ACTION_STOP = "pl.altani.spike.offline.STOP"
        const val EXTRA_GENERATION = "generation"
    }
}
