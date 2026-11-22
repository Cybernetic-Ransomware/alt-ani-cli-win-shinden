package pl.altani.spike.playback

import java.io.InterruptedIOException

/** Closed after `playback.consent_required` until consent or Wi-Fi: no new player transfer may start meanwhile. */
class TransferGate {
    /** The gate as one check saw it; [closures] tells closed periods apart. */
    data class Stamp(val open: Boolean, val closures: Int, val nanos: Long)

    private val lock = Object()
    private var open = true
    private var closures = 0

    val isOpen: Boolean
        get() = synchronized(lock) { open }

    /** Taken under the same lock as [close] and [open], so stamps and gate changes have one order. */
    fun stamp(): Stamp = synchronized(lock) { Stamp(open, closures, System.nanoTime()) }

    fun close(): Stamp =
        synchronized(lock) {
            if (open) {
                open = false
                closures += 1
            }
            Stamp(open, closures, System.nanoTime())
        }

    fun open(): Stamp =
        synchronized(lock) {
            open = true
            lock.notifyAll()
            Stamp(open, closures, System.nanoTime())
        }

    /** Waits until open; the interrupt a Media3 loader sends on cancel or release ends the wait. */
    fun awaitOpen() {
        synchronized(lock) {
            while (!open) {
                try {
                    lock.wait()
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    throw InterruptedIOException("transfer gate wait interrupted")
                }
            }
        }
    }
}
