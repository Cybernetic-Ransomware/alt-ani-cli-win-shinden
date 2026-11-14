package pl.altani.spike

import android.app.Application
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.bridge.ChaquopyEndpoint
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
                    .put("network", currentNetwork())
            },
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
    }

    private fun currentNetwork(): String {
        val manager = getSystemService(ConnectivityManager::class.java)
        val caps = manager.getNetworkCapabilities(manager.activeNetwork) ?: return "offline"
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) -> "wifi"
            else -> "offline"
        }
    }
}
