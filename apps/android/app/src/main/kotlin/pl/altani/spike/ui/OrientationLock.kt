package pl.altani.spike.ui

import android.app.Activity
import android.content.pm.ActivityInfo
import android.provider.Settings
import android.view.OrientationEventListener

/** The fullscreen button turns the screen; once the device is held that way, auto-rotate takes over again. */
class OrientationLock(private val activity: Activity) : OrientationEventListener(activity) {
    fun force(landscape: Boolean) {
        activity.requestedOrientation =
            if (landscape) ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE else ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
    }

    override fun onOrientationChanged(degrees: Int) {
        val forced = when (activity.requestedOrientation) {
            ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE -> true
            ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT -> false
            else -> return
        }
        // with auto-rotate off, letting go would snap back to portrait against the user's choice
        if (autoRotate() && heldAsForced(forced, degrees)) {
            activity.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        }
    }

    private fun autoRotate(): Boolean = Settings.System.getInt(activity.contentResolver, Settings.System.ACCELEROMETER_ROTATION, 0) == 1
}

/** True once the device is clearly held in the forced orientation; tilts in between and a flat device keep the lock. */
fun heldAsForced(landscape: Boolean, degrees: Int): Boolean =
    when {
        degrees == OrientationEventListener.ORIENTATION_UNKNOWN -> false
        landscape -> degrees in 60..120 || degrees in 240..300
        else -> degrees <= 30 || degrees >= 330
    }
