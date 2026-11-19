package pl.altani.spike.bridge

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.DeviceNetwork.CELLULAR
import pl.altani.spike.bridge.DeviceNetwork.OFFLINE
import pl.altani.spike.bridge.DeviceNetwork.WIFI

class DeviceNetworkTest {
    private fun kind(internet: Boolean = true, cellular: Boolean = false, wifi: Boolean = false, notMetered: Boolean = false) =
        DeviceNetwork.classify(internet, cellular, wifi, notMetered)

    @Test
    fun `plain transports keep their kind`() {
        assertEquals(WIFI, kind(wifi = true, notMetered = true))
        assertEquals(WIFI, kind(wifi = true))
        assertEquals(CELLULAR, kind(cellular = true))
        assertEquals(OFFLINE, kind(internet = false, wifi = true))
    }

    @Test
    fun `a vpn counts by what it rides on, and as mobile data when that is unknown and metered`() {
        assertEquals("over LTE, transport reported", CELLULAR, kind(cellular = true, wifi = true, notMetered = true))
        assertEquals("over Wi-Fi, transport reported", WIFI, kind(wifi = true))
        assertEquals("no transport, metered", CELLULAR, kind())
        assertEquals("no transport, unmetered", WIFI, kind(notMetered = true))
    }

    @Test
    fun `the device can only make the bridge's kind stricter`() {
        assertEquals(CELLULAR, DeviceNetwork.stricter(WIFI, CELLULAR))
        assertEquals(OFFLINE, DeviceNetwork.stricter(CELLULAR, OFFLINE))
        assertEquals(CELLULAR, DeviceNetwork.stricter(CELLULAR, WIFI))
        assertEquals(WIFI, DeviceNetwork.stricter(WIFI, null))
    }

    @Test
    fun `mobile data holds only without consent, offline always`() {
        assertTrue(DeviceNetwork.holds(CELLULAR, consented = false))
        assertFalse(DeviceNetwork.holds(CELLULAR, consented = true))
        assertTrue(DeviceNetwork.holds(OFFLINE, consented = true))
        assertFalse(DeviceNetwork.holds(WIFI, consented = false))
    }
}
