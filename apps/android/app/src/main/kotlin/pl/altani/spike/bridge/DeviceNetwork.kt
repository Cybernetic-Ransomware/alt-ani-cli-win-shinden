package pl.altani.spike.bridge

object DeviceNetwork {
    const val WIFI = "wifi"
    const val CELLULAR = "cellular"
    const val OFFLINE = "offline"

    private val STRICTNESS = listOf(WIFI, CELLULAR, OFFLINE)

    /** A VPN reporting no transport counts as mobile data unless unmetered, so a tunnel over LTE never passes for Wi-Fi. */
    fun classify(internet: Boolean, cellular: Boolean, wifiOrEthernet: Boolean, notMetered: Boolean): String =
        when {
            !internet -> OFFLINE
            cellular -> CELLULAR
            wifiOrEthernet -> WIFI
            notMetered -> WIFI
            else -> CELLULAR
        }

    /** The bridge's kind may lag behind the device; whichever holds more wins. */
    fun stricter(bridge: String, device: String?): String =
        if (device != null && STRICTNESS.indexOf(device) > STRICTNESS.indexOf(bridge)) device else bridge

    fun holds(kind: String, consented: Boolean): Boolean = kind == OFFLINE || (kind == CELLULAR && !consented)
}
