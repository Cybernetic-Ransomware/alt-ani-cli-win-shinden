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
import pl.altani.spike.bridge.DeviceNetwork
import pl.altani.spike.bridge.NetworkSync
import pl.altani.spike.controlled.ControlledEnvironment
import pl.altani.spike.offline.AppStorage
import pl.altani.spike.offline.ExecutorControl

/** Owns the process's [BridgeRuntime]; nothing is closed on the way out, the state is durable in SQLite. */
class AltAniApplication : Application() {
    // set by the endpoint factory, which BridgeRuntime runs before engineArgs; a retried create_engine reuses both
    @Volatile private var storageArg: JSONObject? = null

    val bridge: BridgeRuntime by lazy {
        BridgeRuntime(
            endpointFactory = {
                val bridge = Python.getInstance().getModule("android_host").callAttr("create_bridge")
                storageArg = AppStorage.register(bridge, AppStorage.libraryRoot(this))
                ChaquopyEndpoint(bridge)
            },
            engineArgs = {
                JSONObject()
                    .put("cache_dir", cacheDir.absolutePath)
                    .put("history_path", File(filesDir, "alt-ani.db").absolutePath)
                    .put("network", networkSync.kind.value)
                    .put("storage", storageArg)
            },
        )
    }

    /** Nothing in the app starts it yet: no automatic downloads, and no background work is claimed. */
    val executor: ExecutorControl by lazy {
        ExecutorControl(bridge.scope) { method, args -> bridge.call(method, args, mutation = method != "executor_status") }
    }

    /** The real [bridge] only; the controlled runtime stays on loopback "wifi". */
    val networkSync: NetworkSync by lazy {
        NetworkSync(
            initial = currentNetwork(),
            scope = bridge.scope,
            engineRequested = { bridge.engineRequested },
            send = { kind -> bridge.call("network_set", JSONObject().put("kind", kind), mutation = true) },
            log = { Log.i(TAG, it) },
        )
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

                override fun onLost(network: Network) = networkSync.update(DeviceNetwork.OFFLINE)
            },
        )
    }

    private fun currentNetwork(): String {
        val manager = getSystemService(ConnectivityManager::class.java)
        return kindOf(manager.getNetworkCapabilities(manager.activeNetwork))
    }

    private companion object {
        const val TAG = "AltAniNetwork"

        fun kindOf(caps: NetworkCapabilities?): String =
            if (caps == null) {
                DeviceNetwork.OFFLINE
            } else {
                DeviceNetwork.classify(
                    internet = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET),
                    cellular = caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR),
                    wifiOrEthernet = caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                        caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET),
                    notMetered = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED),
                )
            }
    }
}
