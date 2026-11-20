package pl.altani.spike.ui

import java.util.Locale
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject

/** Immutable views of the bridge's catalogue DTOs; JSON stays at the bridge boundary. */
data class SeriesHit(val id: String, val slug: String, val title: String, val url: String, val type: String) {
    fun ref(): SeriesRef = SeriesRef(id, slug, title, url)
}

data class SeriesRef(val id: String, val slug: String, val title: String, val url: String) {
    fun toJson(): JSONObject = JSONObject().put("id", id).put("slug", slug).put("title", title).put("url", url)

    companion object {
        fun parse(json: JSONObject) = SeriesRef(json.getString("id"), json.optString("slug"), json.getString("title"), json.getString("url"))
    }
}

data class Episode(val number: Double, val title: String, val url: String) {
    val label: String
        get() = if (number % 1.0 == 0.0) number.toLong().toString() else number.toString()

    fun toJson(): JSONObject =
        JSONObject().put("number", if (number % 1.0 == 0.0) number.toLong() else number).put("title", title).put("url", url)

    companion object {
        fun parse(json: JSONObject) = Episode(json.getDouble("number"), json.optString("title"), json.getString("url"))
    }
}

data class SearchPage(val query: String, val hits: List<SeriesHit>, val total: Int?, val incompleteReason: String?) {
    companion object {
        fun parse(query: String, data: JSONObject) =
            SearchPage(
                query = query,
                hits = data.getJSONArray("hits").objects().map {
                    SeriesHit(it.getString("id"), it.optString("slug"), it.getString("title"), it.getString("url"), it.optString("series_type"))
                },
                total = if (data.isNull("total")) null else data.optInt("total"),
                incompleteReason = reason(data),
            )
    }
}

data class EpisodePage(val series: SeriesRef, val episodes: List<Episode>, val incompleteReason: String?) {
    companion object {
        fun parse(data: JSONObject) =
            EpisodePage(SeriesRef.parse(data.getJSONObject("series")), data.getJSONArray("episodes").objects().map(Episode::parse), reason(data))
    }
}

/** One player of an episode; the group's URL (`source`) is left out, it is never shown or logged. */
data class Source(val onlineId: String, val player: String, val audio: String, val subs: String, val maxRes: String?, val author: String?) {
    val label: String
        get() = listOfNotNull(player, "audio $audio", "subs $subs", maxRes, author).joinToString(" · ")

    companion object {
        fun parse(json: JSONObject) =
            Source(
                json.getString("online_id"),
                json.getString("player"),
                json.optString("lang_audio"),
                json.optString("lang_subs"),
                json.nullableString("max_res"),
                json.nullableString("subs_author"),
            )
    }
}

/** The bridge's episode session: [sessionId] must go with this episode to `playback_prepare`. */
data class SourcePage(val episode: Episode, val sessionId: String, val sources: List<Source>) {
    companion object {
        fun parse(episode: Episode, data: JSONObject) =
            SourcePage(episode, data.getString("session_id"), data.getJSONArray("sources").objects().map(Source::parse))
    }
}

/** A catalogue pick for the player; a null [onlineId] lets the bridge choose by the preferences, [fromStart] skips the saved position. */
data class PlaybackPick(
    val series: SeriesRef,
    val episode: Episode,
    val sessionId: String,
    val onlineId: String?,
    val fromStart: Boolean = false,
)

/** One episode's row of the engine's watch state, as last read; a missing row comes back as revision 0 with nothing known. */
data class EpisodeWatch(
    val seriesId: String,
    val number: Double,
    val revision: Int,
    val lastOpenedAt: Double?,
    val positionMs: Long?,
    val durationMs: Long?,
    val watched: Boolean,
) {
    val nearEnd: Boolean
        get() = positionMs != null && durationMs != null && positionMs >= durationMs - NEAR_END_MS

    /** A watched episode saved at its end would only end again; starting over is then the user's own choice. */
    val resumable: Boolean
        get() = (positionMs ?: 0) > 0 && !(watched && nearEnd)

    val label: String
        get() = if (number % 1.0 == 0.0) number.toLong().toString() else number.toString()

    companion object {
        const val NEAR_END_MS = 5_000L

        fun parse(json: JSONObject) =
            EpisodeWatch(
                json.getString("series_id"),
                json.getDouble("number"),
                json.optInt("revision"),
                if (json.isNull("last_opened_at")) null else json.getDouble("last_opened_at"),
                json.nullableLong("position_ms"),
                json.nullableLong("duration_ms"),
                json.optBoolean("watched"),
            )
    }
}

/** A `watch_recent` row: the series and the state of its last opened episode. */
data class RecentSeries(val series: SeriesRef, val last: EpisodeWatch) {
    companion object {
        fun parsePage(data: JSONObject): List<RecentSeries> =
            data.getJSONArray("series").objects().map {
                RecentSeries(SeriesRef.parse(it.getJSONObject("series")), EpisodeWatch.parse(it.getJSONObject("watch")))
            }
    }
}

/** A `watch_series` reply; [episodes] holds only the episodes the engine has a row for. */
data class SeriesWatch(val seriesId: String, val lastOpened: EpisodeWatch?, val episodes: Map<Double, EpisodeWatch>) {
    companion object {
        fun parse(seriesId: String, data: JSONObject): SeriesWatch {
            val last = if (data.isNull("last_opened")) null else EpisodeWatch.parse(data.getJSONObject("last_opened"))
            val rows = data.getJSONArray("episodes").objects().map(EpisodeWatch::parse)
            if (rows.any { it.seriesId != seriesId } || (last != null && last.seriesId != seriesId)) throw JSONException("other series")
            return SeriesWatch(seriesId, last, rows.associateBy { it.number })
        }
    }
}

fun clock(ms: Long): String {
    val total = ms / 1000
    val (h, m, s) = Triple(total / 3600, total / 60 % 60, total % 60)
    return if (h > 0) String.format(Locale.ROOT, "%d:%02d:%02d", h, m, s) else String.format(Locale.ROOT, "%d:%02d", m, s)
}

private fun JSONObject.nullableLong(name: String): Long? = if (isNull(name)) null else getLong(name)

private fun JSONObject.nullableString(name: String): String? = if (isNull(name)) null else optString(name).ifEmpty { null }

private fun reason(data: JSONObject): String? =
    if (data.optBoolean("complete", true)) null else data.optString("incomplete_reason").ifEmpty { "unknown" }

private fun JSONArray.objects(): List<JSONObject> = (0 until length()).map(::getJSONObject)
