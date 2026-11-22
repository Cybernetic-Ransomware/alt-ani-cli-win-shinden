package pl.altani.spike.offline.background

import android.app.job.JobParameters
import android.app.job.JobService
import android.os.PersistableBundle
import androidx.annotation.RequiresApi
import java.util.concurrent.ConcurrentHashMap
import pl.altani.spike.AltAniApplication

/** `jobFinished` only from the session's normal end; after `onStopJob` a late drain never reaches the host. */
@RequiresApi(34)
class TransferJobService : JobService() {
    private val app get() = application as AltAniApplication
    private val hosts = ConcurrentHashMap<Int, JobHost>()

    private inner class JobHost(private val params: JobParameters) : RunHost {
        @Volatile var generation = OfflineRun.REJECTED
        @Volatile var stopped = false

        override val path = RunPath.USER_INITIATED_JOB

        override fun show(generation: Long, model: NotificationModel) {
            if (!stopped) app.notifications.showSession(model, generation)
        }

        override fun setWorking(working: Boolean) = Unit

        @Synchronized
        override fun finish(reschedule: Boolean) {
            if (stopped) return
            stopped = true
            hosts.remove(params.jobId, this)
            jobFinished(params, reschedule)
        }

        @Synchronized
        fun stop(): Long {
            stopped = true
            hosts.remove(params.jobId, this)
            return generation
        }
    }

    override fun onStartJob(params: JobParameters): Boolean {
        // within 10 s of onStartJob or the system raises an ANR and stops the job; built without any bridge call
        setNotification(
            params,
            DownloadNotifications.NOTIFICATION_RUN,
            app.notifications.session(NotificationModels.preparing, null),
            JOB_END_NOTIFICATION_POLICY_REMOVE,
        )
        val spec = launchSpec(params.extras)
        val host = JobHost(params)
        hosts[params.jobId] = host
        val generation = app.offlineRun.attach(host, spec.target, spec.initiatedAtMs, spec.scope)
        if (generation == OfflineRun.REJECTED) {
            host.stop()
            return false
        }
        host.generation = generation
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean {
        val host = hosts[params.jobId] ?: return false
        val generation = host.stop()
        val reason = when (params.stopReason) {
            JobParameters.STOP_REASON_USER -> SystemStop.USER
            JobParameters.STOP_REASON_CONSTRAINT_CONNECTIVITY -> SystemStop.CONNECTIVITY
            else -> SystemStop.OTHER
        }
        return app.offlineRun.onSystemStop(generation, reason)
    }

    companion object {
        const val JOB_ID = 52_001

        fun launchSpec(extras: PersistableBundle): LaunchSpec =
            LaunchSpec.fromExtras { key ->
                when {
                    !extras.containsKey(key) -> null
                    key == LaunchSpec.KEY_INITIATED_AT -> extras.getLong(key)
                    else -> extras.getString(key)
                }
            }
    }
}
