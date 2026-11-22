package pl.altani.spike.offline.background

import android.content.Context
import android.os.PowerManager

/** Foreground-service path only: a partial lock while transfers run, renewed by each read, never held while waiting. */
class RunWakeLock(context: Context) {
    private val lock = context.getSystemService(PowerManager::class.java)
        .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "altani:offline-run")
        .apply { setReferenceCounted(false) }

    @Synchronized
    fun set(working: Boolean) {
        if (working) {
            // the coordinator reads at least every minute; the timeout only bounds a lock nobody releases
            lock.acquire(TIMEOUT_MS)
        } else if (lock.isHeld) {
            lock.release()
        }
    }

    private companion object {
        const val TIMEOUT_MS = 10 * 60_000L
    }
}
