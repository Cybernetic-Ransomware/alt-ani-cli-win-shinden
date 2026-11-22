package pl.altani.spike.ui

import org.json.JSONException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.ui.FakeCalls.Companion.seriesWatch
import pl.altani.spike.ui.FakeCalls.Companion.watch

class CatalogModelsTest {
    @Test
    fun `a missing row parses as nothing known`() {
        val parsed = EpisodeWatch.parse(watch("1", 2, opened = null, revision = 0))
        assertEquals(EpisodeWatch("1", 2.0, 0, null, null, null, false), parsed)
        assertFalse(parsed.resumable)
        assertFalse(parsed.nearEnd)
    }

    @Test
    fun `only a watched episode saved at its end stops being resumable`() {
        assertTrue(EpisodeWatch.parse(watch("1", 1, 60_000, 1_440_000)).resumable)
        assertTrue("near the end but not watched", EpisodeWatch.parse(watch("1", 1, 1_438_000, 1_440_000)).resumable)
        assertTrue("watched but stopped earlier", EpisodeWatch.parse(watch("1", 1, 60_000, 1_440_000, watched = true)).resumable)
        val end = EpisodeWatch.parse(watch("1", 1, 1_435_000, 1_440_000, watched = true))
        assertTrue(end.nearEnd)
        assertFalse(end.resumable)
        assertFalse("no duration, no end known", EpisodeWatch.parse(watch("1", 1, 60_000)).nearEnd)
    }

    @Test
    fun `series watch keys rows by number and refuses another series`() {
        val parsed = SeriesWatch.parse("1", seriesWatch(watch("1", 12.5, 1_000), watch("1", 12.5, 1_000), watch("1", 1)).data)
        assertEquals(12.5, parsed.lastOpened!!.number, 0.0)
        assertEquals(setOf(12.5, 1.0), parsed.episodes.keys)
        assertNull(SeriesWatch.parse("1", seriesWatch(null).data).lastOpened)

        val wrong = runCatching { SeriesWatch.parse("1", seriesWatch(null, watch("2", 1)).data) }
        assertTrue(wrong.exceptionOrNull() is JSONException)
    }

    @Test
    fun `clock shows minutes, and hours from an hour up`() {
        assertEquals("0:00", clock(0))
        assertEquals("12:34", clock(754_999))
        assertEquals("1:02:03", clock(3_723_000))
    }
}
