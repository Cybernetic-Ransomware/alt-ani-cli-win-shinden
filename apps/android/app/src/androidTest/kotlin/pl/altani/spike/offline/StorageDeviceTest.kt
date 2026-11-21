package pl.altani.spike.offline

import android.os.Build
import android.util.Log
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chaquo.python.Python
import java.io.File
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.AltAniApplication
import pl.altani.spike.bridge.BridgeResult

/** P1 and P2: the production bridge has its file store, and the store keeps its guarantees on this device's storage. */
@RunWith(AndroidJUnit4::class)
class StorageDeviceTest {
    @get:Rule
    val outcomes = DeviceOutcomes()

    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val probeDir = File(app.filesDir, "probe-${UUID.randomUUID()}")

    @After
    fun tearDown() {
        probeDir.deleteRecursively()
    }

    @Test
    fun theAppBridgeHasAFileStore() {
        // library_verify needs the store and touches no network; without one it is storage.not_configured
        val reply = runBlocking { app.bridge.call("library_verify", JSONObject()) }
        assertTrue("library_verify: $reply", reply is BridgeResult.Ok)
    }

    @Test
    fun theStoreKeepsItsGuaranteesOnThisDevice() {
        val module = Python.getInstance().getModule("storage_probe")
        val report = JSONObject(module.callAttr("run", probeDir.absolutePath, Build.VERSION.SDK_INT).toString())
        val checks = report.getJSONObject("checks")
        val failed = checks.keys().asSequence().filterNot { checks.getJSONObject(it).getBoolean("ok") }.toList()
        checks.keys().forEach { Log.i(DeviceOutcomes.TAG, "probe $it ${checks.getJSONObject(it)}") }
        assertEquals("failed probe checks: ${failed.associateWith { checks.getJSONObject(it) }}", emptyList<String>(), failed)
        val expected = if (Build.VERSION.SDK_INT >= 30) 11 else 2
        assertEquals(expected, checks.length())
    }
}
