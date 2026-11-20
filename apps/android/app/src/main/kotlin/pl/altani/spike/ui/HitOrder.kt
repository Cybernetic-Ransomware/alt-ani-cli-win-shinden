package pl.altani.spike.ui

import java.text.Normalizer
import org.json.JSONException
import org.json.JSONObject

enum class HitOrder {
    RELEVANCE,
    NEWEST;

    companion object {
        private val MARKS = Regex("\\p{M}+")
        private val SEPARATORS = Regex("[^\\p{L}\\p{N}]+")
        private val TYPES = listOf("tv", "ona", "ova", "film", "odcinek specjalny")

        fun relevance(query: String, hits: List<SeriesHit>): List<SeriesHit> {
            val q = normalize(query)
            return hits.sortedWith(compareBy({ match(normalize(it.title), q) }, { typeRank(it.type) }, { normalize(it.title) }))
        }

        /** Newest first; titles without a known date keep their order at the end. */
        fun newest(hits: List<SeriesHit>, dates: Map<String, AirDate?>): List<SeriesHit> =
            hits.sortedWith(compareBy(nullsLast(reverseOrder<AirDate>())) { dates[it.id] })

        private fun match(title: String, query: String): Int {
            if (query.isEmpty()) return 5
            val words = query.split(' ')
            return when {
                title == query -> 0
                title.startsWith(query) -> 1
                " $title ".contains(" $query ") -> 2
                title.contains(query) -> 3
                words.all { it in title } -> 4
                else -> 5
            }
        }

        private fun typeRank(type: String): Int =
            TYPES.indexOf(type.lowercase()).let { if (it < 0) TYPES.size else it }

        private fun normalize(text: String): String =
            Normalizer.normalize(text, Normalizer.Form.NFD).replace(MARKS, "").lowercase().replace('ł', 'l').replace(SEPARATORS, " ").trim()
    }
}

/** Shinden's `air_date_sort`; an unknown month or day is 0, as in the engine. */
data class AirDate(val year: Int, val month: Int, val day: Int) : Comparable<AirDate> {
    override fun compareTo(other: AirDate): Int = compareValuesBy(this, other, AirDate::year, AirDate::month, AirDate::day)

    companion object {
        /** One entry of `metadata_many`; null when the series was not read or its date is unknown. */
        fun parse(metadata: JSONObject?): AirDate? {
            val parts = metadata?.optJSONArray("air_date_sort") ?: return null
            if (parts.length() != 3) throw JSONException("air_date_sort")
            return AirDate(parts.getInt(0), parts.getInt(1), parts.getInt(2))
        }
    }
}
