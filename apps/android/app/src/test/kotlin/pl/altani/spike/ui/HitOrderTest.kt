package pl.altani.spike.ui

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HitOrderTest {
    private fun hit(id: String, title: String, type: String = "TV") = SeriesHit(id, "s$id", title, "https://shinden.pl/series/$id", type)

    private fun List<SeriesHit>.ids() = map { it.id }

    @Test
    fun `relevance puts exact, prefix and whole-word matches before loose ones`() {
        val hits = listOf(
            hit("other", "Kusuriya no Hitorigoto"),
            hit("scattered", "Frieren: Sousou no Mahou"),
            hit("substring", "XSousou no Frierenx"),
            hit("words", "The Sousou no Frieren Special"),
            hit("prefix", "Sousou no Frieren 2nd Season"),
            hit("exact", "Sousou no Frieren"),
        )
        assertEquals(
            listOf("exact", "prefix", "words", "substring", "scattered", "other"),
            HitOrder.relevance("  sousou NO frieren ", hits).ids(),
        )
    }

    @Test
    fun `ties go to TV before films and specials, then by title`() {
        val hits = listOf(
            hit("special", "Frieren Mini", "Odcinek Specjalny"),
            hit("film", "Frieren Movie", "Film"),
            hit("tv2", "Frieren 2", "TV"),
            hit("unknown", "Frieren Music", "Music"),
            hit("tv1", "Frieren 1", "TV"),
        )
        assertEquals(listOf("tv1", "tv2", "film", "special", "unknown"), HitOrder.relevance("frieren", hits).ids())
    }

    @Test
    fun `accents and punctuation do not break a match`() {
        val hits = listOf(hit("b", "Zupełnie inny"), hit("a", "Shingeki no Kyojin: Kanketsu-hen"))
        assertEquals(listOf("a", "b"), HitOrder.relevance("shingeki no kyojin kanketsu hen", hits).ids())
        assertEquals(listOf("b", "a"), HitOrder.relevance("zupelnie", hits).ids())
    }

    @Test
    fun `newest first keeps undated titles last in their current order`() {
        val hits = listOf(hit("u1", "A"), hit("old", "B"), hit("new", "C"), hit("u2", "D"), hit("year", "E"))
        val dates = mapOf("old" to AirDate(2010, 4, 1), "new" to AirDate(2023, 9, 29), "year" to AirDate(2023, 0, 0), "u1" to null)
        assertEquals(listOf("new", "year", "old", "u1", "u2"), HitOrder.newest(hits, dates).ids())
    }

    @Test
    fun `an air date is read from a metadata entry`() {
        assertEquals(AirDate(2023, 9, 29), AirDate.parse(JSONObject().put("air_date_sort", JSONArray(listOf(2023, 9, 29)))))
        assertNull(AirDate.parse(JSONObject().put("air_date_sort", JSONObject.NULL)))
        assertNull(AirDate.parse(null))
    }
}
