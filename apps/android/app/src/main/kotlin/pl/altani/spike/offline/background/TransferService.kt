package pl.altani.spike.offline.background

import android.app.Notification
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import androidx.core.app.ServiceCompat
import pl.altani.spike.AltAniApplication

/** Never sticky: a killed process is not restarted by the system, the next [OfflineRun.request] recovers. */
class TransferService : Service() {
    private val app get() = application as AltAniApplication
    private lateinit var wakeLock: RunWakeLock
    @Volatile private var generation = OfflineRun.REJECTED
    @Volatile private var shown: Notification? = null
    @Volatile private var finished = false

    private val host = object : RunHost {
        override val path = RunPath.FOREGROUND_SERVICE

        override fun show(generation: Long, model: NotificationModel) {
            if (!finished) shown = app.notifications.showSession(model, generation)
        }

        override fun setWorking(working: Boolean) {
            if (!finished) wakeLock.set(working)
        }

        override fun finish(reschedule: Boolean) = end()
    }

    override fun onCreate() {
        super.onCreate()
        wakeLock = RunWakeLock(this)
        running = true
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // every startForegroundService call needs its startForeground, a repeated one included
        val notification = shown ?: app.notifications.session(NotificationModels.preparing, null)
        ServiceCompat.startForeground(this, DownloadNotifications.NOTIFICATION_RUN, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        if (finished) {
            end()
        } else if (generation == OfflineRun.REJECTED) {
            val spec = launchSpec(intent)
            generation = app.offlineRun.attach(host, spec.target, spec.initiatedAtMs, spec.scope)
            if (generation == OfflineRun.REJECTED) end()
        }
        return START_NOT_STICKY
    }

    /** API 35+ `dataSync` limit; this path runs below 34, so only a defensive stop within the allowed seconds. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        app.offlineRun.onHostGone(generation)
        end()
    }

    override fun onDestroy() {
        finished = true
        wakeLock.set(false)
        running = false
        app.offlineRun.onHostGone(generation)
        super.onDestroy()
    }

    private fun end() {
        finished = true
        wakeLock.set(false)
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    companion object {
        fun launchSpec(intent: Intent?): LaunchSpec =
            LaunchSpec.fromExtras { key ->
                val extras = intent?.extras
                when {
                    extras == null || !extras.containsKey(key) -> null
                    key == LaunchSpec.KEY_INITIATED_AT -> extras.getLong(key)
                    else -> extras.getString(key)
                }
            }

        /** Read by the launcher so a second request never starts a second session; set at launch, before `onCreate`. */
        @Volatile var running = false
            internal set
    }
}
