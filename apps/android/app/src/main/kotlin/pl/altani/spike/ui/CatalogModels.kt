package pl.altani.spike.ui

import org.json.JSONArray
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

private fun reason(data: JSONObject): String? =
    if (data.optBoolean("complete", true)) null else data.optString("incomplete_reason").ifEmpty { "unknown" }

private fun JSONArray.objects(): List<JSONObject> = (0 until length()).map(::getJSONObject)
