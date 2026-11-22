package pl.altani.spike.playback

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.playback.PositionReporter.Write

class WatchSavedTest {
    private val session = JSONObject().put("id", "v1").put("series_id", "7").put("number", 12.5)

    private fun reply(accepted: Boolean) = BridgeResult.Ok(JSONObject().put("accepted", accepted))

    @Test
    fun `an accepted write names its own viewing`() {
        assertEquals(
            WatchSaved("7", 12.5, "v1", periodic = false, ended = false),
            WatchSaved.of(session, Write(PositionReporter.POSITION, 1, 1_000, null), reply(true)),
        )
        assertEquals(true, WatchSaved.of(session, Write(PositionReporter.POSITION, 2, 2_000, null, periodic = true), reply(true))?.periodic)
        assertEquals(
            WatchSaved("7", 12.5, "v1", periodic = false, ended = true),
            WatchSaved.of(session, Write(PositionReporter.ENDED, 3, 3_000, 3_000), reply(true)),
        )
    }

    @Test
    fun `a refused or failed write, or a close, is not a save`() {
        assertNull(WatchSaved.of(session, Write(PositionReporter.ENDED, 3, 3_000, 3_000), reply(false)))
        assertNull(WatchSaved.of(session, Write(PositionReporter.POSITION, 1, 1_000, null), BridgeResult.Ok(JSONObject())))
        assertNull(WatchSaved.of(session, Write(PositionReporter.POSITION, 1, 1_000, null), BridgeResult.Failure("cancelled", "cancelled", false, JSONObject())))
        assertNull(WatchSaved.of(session, Write(PositionReporter.CLOSE, 0, 0, null), BridgeResult.Ok(JSONObject().put("closed", true))))
    }
}
