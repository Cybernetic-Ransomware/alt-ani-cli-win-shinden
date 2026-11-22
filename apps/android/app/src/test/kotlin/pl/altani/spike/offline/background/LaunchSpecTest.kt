package pl.altani.spike.offline.background

import android.net.NetworkCapabilities
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

class LaunchSpecTest {
    @Test
    fun `the extras carry target, scope and the decision time`() {
        val spec = LaunchSpec(RunTarget.CONTROLLED, NetworkScope.ANY, 42L, estimatedBytes = 9)
        val extras = spec.extras()
        assertEquals(spec.copy(estimatedBytes = null), LaunchSpec.fromExtras(extras::get))
    }

    @Test
    fun `missing or unknown extras fall back to the strict choice`() {
        val spec = LaunchSpec.fromExtras { if (it == LaunchSpec.KEY_TARGET) "SOMETHING" else null }
        assertEquals(RunTarget.APP, spec.target)
        assertEquals(NetworkScope.WIFI, spec.scope)
        assertFalse(RunPolicy.withinWindow(spec.initiatedAtMs, 0))
        assertNull(spec.estimatedBytes)
    }

    @Test
    fun `the path follows the API level with nothing below 30`() {
        assertNull(PlatformLauncher.pathFor(29))
        assertEquals(RunPath.FOREGROUND_SERVICE, PlatformLauncher.pathFor(30))
        assertEquals(RunPath.FOREGROUND_SERVICE, PlatformLauncher.pathFor(33))
        assertEquals(RunPath.USER_INITIATED_JOB, PlatformLauncher.pathFor(34))
        assertEquals(RunPath.USER_INITIATED_JOB, PlatformLauncher.pathFor(36))
    }

    @Test
    fun `wifi only means a Wi-Fi or Ethernet transport, never just an unmetered network`() {
        assertEquals(
            listOf(NetworkCapabilities.TRANSPORT_WIFI, NetworkCapabilities.TRANSPORT_ETHERNET),
            PlatformLauncher.transportsFor(NetworkScope.WIFI),
        )
        assertEquals(emptyList<Int>(), PlatformLauncher.transportsFor(NetworkScope.ANY))
    }
}
