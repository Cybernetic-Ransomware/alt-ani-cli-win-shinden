package pl.altani.spike

import android.app.Application
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.bridge.ChaquopyEndpoint
import pl.altani.spike.bridge.NetworkSync
import pl.altani.spike.controlled.ControlledEnvironment

/** Owns the process's [BridgeRuntime]; nothing is closed on the way out, the state is durable in SQLite. */
class AltAniApplication : Application() {
    val bridge: BridgeRuntime by lazy {
        BridgeRuntime(
            endpointFactory = {
                ChaquopyEndpoint(Python.getInstance().getModule("android_host").callAttr("create_bridge"))
            },
            engineArgs = {
                JSONObject()
                    .put("cache_dir", cacheDir.absolutePath)
                    .put("history_path", File(filesDir, "alt-ani.db").absolutePath)
                    .put("network", networkSync.kind.value)
            },
        )
    }

    /** The real [bridge] only; the controlled runtime stays on loopback "wifi". */
    val networkSync: NetworkSync by lazy {
        NetworkSync(
            initial = currentNetwork(),
            scope = bridge.scope,
            engineRequested = { bridge.engineRequested },
            send = { kind -> bridge.call("network_set", JSONObject().put("kind", kind), mutation = true) },
        ) { Log.i(TAG, it) }
    }

    /** Debug builds only: the technical screen's controlled playback, separate from [bridge] and its database. */
    val controlledBridge: BridgeRuntime? by lazy {
        if (BuildConfig.DEBUG) ControlledEnvironment.runtime(this, "app") else null
    }

    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        // once per process: an Activity recreated by rotation never registers a second monitor
        getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(
            object : ConnectivityManager.NetworkCallback() {
                override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = networkSync.update(kindOf(caps))

                override fun onLost(network: Network) = networkSync.update(OFFLINE)
            },
        )
    }

    private fun currentNetwork(): String {
        val manager = getSystemService(ConnectivityManager::class.java)
        return kindOf(manager.getNetworkCapabilities(manager.activeNetwork))
    }

    private companion object {
        const val TAG = "AltAniNetwork"
        const val OFFLINE = "offline"

        fun kindOf(caps: NetworkCapabilities?): String =
            when {
                caps == null -> OFFLINE
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
                caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) -> "wifi"
                else -> OFFLINE
            }
    }
}
