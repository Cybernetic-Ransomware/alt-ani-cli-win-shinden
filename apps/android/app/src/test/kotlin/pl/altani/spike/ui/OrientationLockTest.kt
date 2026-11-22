package pl.altani.spike.ui

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OrientationLockTest {
    @Test
    fun `a forced landscape is let go only when the device is held sideways`() {
        listOf(90, 60, 120, 270, 240, 300).forEach { assertTrue("$it°", heldAsForced(landscape = true, it)) }
        listOf(0, 45, 135, 180, 315, 359).forEach { assertFalse("$it°", heldAsForced(landscape = true, it)) }
    }

    @Test
    fun `a forced portrait is let go only when the device is held upright`() {
        listOf(0, 30, 330, 359).forEach { assertTrue("$it°", heldAsForced(landscape = false, it)) }
        listOf(45, 90, 180, 270, 315).forEach { assertFalse("$it°", heldAsForced(landscape = false, it)) }
    }

    @Test
    fun `a flat device keeps the lock`() {
        assertFalse(heldAsForced(landscape = true, -1))
        assertFalse(heldAsForced(landscape = false, -1))
    }
}
