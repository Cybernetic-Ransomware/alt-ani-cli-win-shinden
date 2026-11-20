package pl.altani.spike.playback

import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult

/** A position or end the engine accepted for one viewing; identified by its own playback session, never by what is on screen. */
data class WatchSaved(val seriesId: String, val number: Double, val playbackSessionId: String, val periodic: Boolean, val ended: Boolean) {
    companion object {
        /** Null for `playback_close`, a refused write (`accepted: false`) or a failure. */
        fun of(session: JSONObject, write: PositionReporter.Write, result: BridgeResult): WatchSaved? {
            if (write.method != PositionReporter.POSITION && write.method != PositionReporter.ENDED) return null
            if (result !is BridgeResult.Ok || !result.data.optBoolean("accepted")) return null
            return WatchSaved(
                session.getString("series_id"),
                session.getDouble("number"),
                session.getString("id"),
                periodic = write.periodic,
                ended = write.method == PositionReporter.ENDED,
            )
        }
    }
}
