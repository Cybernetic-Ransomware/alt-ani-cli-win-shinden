package pl.altani.spike.offline.background

import android.app.job.JobInfo
import android.app.job.JobScheduler
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import android.os.PersistableBundle
import android.util.Log
import androidx.annotation.RequiresApi
import androidx.core.content.ContextCompat

/** UIDT from API 34, a `dataSync` foreground service on 30–33; no fallback from one to the other. */
class PlatformLauncher(private val context: Context) : RunLauncher {
    override val path: RunPath? = pathFor(Build.VERSION.SDK_INT)

    override fun isActive(): Boolean =
        when (path) {
            // a running job is listed too, and scheduling the same id again would stop it
            RunPath.USER_INITIATED_JOB -> scheduler().getPendingJob(TransferJobService.JOB_ID) != null
            RunPath.FOREGROUND_SERVICE -> TransferService.running
            null -> false
        }

    override fun launch(spec: LaunchSpec): Boolean =
        try {
            when (path) {
                RunPath.USER_INITIATED_JOB -> Build.VERSION.SDK_INT >= 34 && schedule(spec)
                RunPath.FOREGROUND_SERVICE -> {
                    val intent = Intent(context, TransferService::class.java)
                    spec.extras().forEach { (key, value) ->
                        when (value) {
                            is Long -> intent.putExtra(key, value)
                            else -> intent.putExtra(key, value.toString())
                        }
                    }
                    ContextCompat.startForegroundService(context, intent)
                    true
                }
                null -> false
            }
        } catch (e: IllegalStateException) {
            // ForegroundServiceStartNotAllowedException (API 31+) is one: the app was not visible
            Log.w(TAG, "launch refused: ${e.javaClass.simpleName}")
            false
        } catch (e: SecurityException) {
            Log.w(TAG, "launch refused: ${e.javaClass.simpleName}")
            false
        }

    @RequiresApi(34)
    private fun schedule(spec: LaunchSpec): Boolean {
        val extras = PersistableBundle()
        spec.extras().forEach { (key, value) ->
            when (value) {
                is Long -> extras.putLong(key, value)
                else -> extras.putString(key, value.toString())
            }
        }
        val network = NetworkRequest.Builder().addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        transportsFor(spec.scope).forEach { network.addTransportType(it) }
        val job = JobInfo.Builder(TransferJobService.JOB_ID, ComponentName(context, TransferJobService::class.java))
            .setUserInitiated(true)
            .setRequiredNetwork(network.build())
            // a reboot or force-stop ends it for good; only the user starts the next session
            .setPersisted(false)
            .setBackoffCriteria(BACKOFF_MS, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
            .setExtras(extras)
            .apply { spec.estimatedBytes?.let { setEstimatedNetworkBytes(it, JobInfo.NETWORK_BYTES_UNKNOWN.toLong()) } }
            .build()
        return scheduler().schedule(job) == JobScheduler.RESULT_SUCCESS
    }

    private fun scheduler() = context.getSystemService(JobScheduler::class.java)

    companion object {
        private const val TAG = "AltAniOfflineRun"
        private const val BACKOFF_MS = 60_000L

        fun pathFor(sdk: Int): RunPath? =
            when {
                sdk >= 34 -> RunPath.USER_INITIATED_JOB
                sdk >= 30 -> RunPath.FOREGROUND_SERVICE
                else -> null
            }

        /** `wifi_only` is Wi-Fi or Ethernet transport, as `DeviceNetwork.classify` counts it, not an unmetered network. */
        fun transportsFor(scope: NetworkScope): List<Int> =
            when (scope) {
                NetworkScope.WIFI -> listOf(NetworkCapabilities.TRANSPORT_WIFI, NetworkCapabilities.TRANSPORT_ETHERNET)
                NetworkScope.ANY -> emptyList()
            }
    }
}
