package pl.altani.spike.ui

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.playback.WatchSaved
import pl.altani.spike.ui.CatalogFlow.Load
import pl.altani.spike.ui.FakeCalls.Companion.episodes
import pl.altani.spike.ui.FakeCalls.Companion.failure
import pl.altani.spike.ui.FakeCalls.Companion.hit
import pl.altani.spike.ui.FakeCalls.Companion.hits
import pl.altani.spike.ui.FakeCalls.Companion.recent
import pl.altani.spike.ui.FakeCalls.Companion.seriesWatch
import pl.altani.spike.ui.FakeCalls.Companion.sources
import pl.altani.spike.ui.FakeCalls.Companion.watch

@OptIn(ExperimentalCoroutinesApi::class)
class CatalogHistoryTest {
    private val fake = FakeCalls()

    private fun TestScope.flow() = CatalogFlow(backgroundScope, fake::call, clock = { 0L })

    private fun <T> Load<T>.ready(): T = (this as Load.Ready<T>).value

    private fun count(method: String) = fake.calls.count { it.method == method }

    private fun saved(seriesId: String, number: Double = 3.0, periodic: Boolean = false, ended: Boolean = false) =
        WatchSaved(seriesId, number, "viewing-$seriesId", periodic, ended)

    private fun TestScope.history(flow: CatalogFlow, vararg rows: org.json.JSONObject) {
        flow.refreshRecent()
        runCurrent()
        fake.last("watch_recent").answer(recent(*rows))
        runCurrent()
    }

    /** Series 1 from the history, its listing and its watch state answered. */
    private fun TestScope.opened(flow: CatalogFlow, last: org.json.JSONObject?, vararg rows: org.json.JSONObject, numbers: Array<Number> = arrayOf(1, 2, 3)) {
        history(flow, watch("1", 3, 60_000, 1_440_000))
        flow.selectSeries(flow.state.value.recent.ready().single().series)
        runCurrent()
        fake.last("episodes").answer(episodes("1", *numbers))
        fake.last("watch_series").answer(seriesWatch(last, *rows))
        runCurrent()
    }

    @Test
    fun `an empty history is ready, not failed`() = runTest {
        val flow = flow()
        flow.refreshRecent()
        runCurrent()
        assertEquals(Load.Loading, flow.state.value.recent)
        assertEquals(CatalogFlow.RECENT_LIMIT, fake.last("watch_recent").args.getInt("limit"))
        fake.last("watch_recent").answer(recent())
        runCurrent()
        assertTrue(flow.state.value.recent.ready().isEmpty())
    }

    @Test
    fun `history rows keep the engine order with each series' last episode`() = runTest {
        val flow = flow()
        history(flow, watch("3", 7, 754_000, 1_440_000), watch("1", 2, watched = true), watch("2", 12.5))
        val rows = flow.state.value.recent.ready()
        assertEquals(listOf("3", "1", "2"), rows.map { it.series.id })
        assertEquals(SeriesRef("3", "slug-3", "Title 3", "https://shinden.pl/series/3-slug-3"), rows[0].series)
        assertEquals(754_000L, rows[0].last.positionMs)
        assertTrue(rows[1].last.watched)
        assertEquals("12.5", rows[2].last.label)
    }

    @Test
    fun `a series opened from the history lists its episodes and watch state without a search`() = runTest {
        val flow = flow()
        history(flow, watch("1", 3))
        flow.selectSeries(flow.state.value.recent.ready().single().series)
        runCurrent()

        assertEquals(0, count("search"))
        assertEquals(setOf("id", "slug", "title", "url"), fake.last("episodes").args.getJSONObject("series").keys().asSequence().toSet())
        assertEquals("1", fake.last("watch_series").args.getString("series_id"))
        assertEquals(Load.Loading, flow.state.value.watch)
        assertFalse("the history read does not make the catalogue busy", flow.state.value.copy(episodes = Load.Idle).busy)
    }

    @Test
    fun `the last opened episode and the watched marks come from watch_series`() = runTest {
        val flow = flow()
        opened(
            flow,
            watch("1", 3, 60_000, 1_440_000),
            watch("1", 1, 1_440_000, 1_440_000, watched = true),
            watch("1", 3, 60_000, 1_440_000),
            watch("1", 12.5, 1_000, opened = null),
            numbers = arrayOf(1, 2, 3, 12.5),
        )
        val known = flow.state.value.watch.ready()
        assertEquals(3.0, known.lastOpened!!.number, 0.0)
        assertTrue(known.lastOpened!!.resumable)
        assertTrue(known.episodes.getValue(1.0).watched)
        assertNull("no row, nothing known", known.episodes[2.0])
        assertEquals(1_000L, known.episodes.getValue(12.5).positionMs)
    }

    @Test
    fun `resume goes through the sources screen and only the user's choice starts over`() = runTest {
        val flow = flow()
        opened(flow, watch("1", 3, 60_000, 1_440_000), watch("1", 3, 60_000, 1_440_000))

        flow.resume(fromStart = false)
        runCurrent()
        assertEquals(3L, fake.last("open_episode").args.getJSONObject("episode").get("number"))
        assertEquals(0, count("playback_begin"))
        fake.last("open_episode").answer(sources("s1", "11"))
        runCurrent()
        assertEquals(60_000L, flow.state.value.episodeWatch!!.positionMs)
        assertFalse(flow.pickSource(null)!!.fromStart)

        flow.resume(fromStart = true)
        runCurrent()
        assertTrue("shown before any pick", flow.state.value.fromStart)
        fake.last("open_episode").answer(sources("s2", "11"))
        runCurrent()
        val pick = flow.pickSource(null)!!
        assertTrue(pick.fromStart)
        assertEquals("s2", pick.sessionId)
        assertFalse("the choice is used by one pick only", flow.state.value.fromStart)

        flow.startFrom(beginning = true)
        assertTrue(flow.pickSource(flow.state.value.sources.ready().sources.single())!!.fromStart)
    }

    @Test
    fun `a watched episode at its end is never started over by itself`() = runTest {
        val flow = flow()
        val end = watch("1", 3, 1_438_000, 1_440_000, watched = true)
        opened(flow, end, end)
        val last = flow.state.value.watch.ready().lastOpened!!
        assertTrue(last.nearEnd)
        assertFalse(last.resumable)

        flow.openEpisode(flow.state.value.episodes.ready().episodes[2])
        runCurrent()
        fake.last("open_episode").answer(sources("s1", "11"))
        runCurrent()
        assertFalse(flow.state.value.fromStart)
        assertFalse(flow.pickSource(null)!!.fromStart)
    }

    @Test
    fun `resume does nothing when the last episode is off the listing or nothing was opened`() = runTest {
        val flow = flow()
        opened(flow, watch("1", 3, 60_000), numbers = arrayOf(1, 2))
        flow.resume(fromStart = false)
        runCurrent()
        assertEquals(0, count("open_episode"))
        assertNull(flow.state.value.episode)

        flow.back()
        runCurrent()
        flow.selectSeries(SeriesRef("2", "slug-2", "Title 2", "https://shinden.pl/series/2-slug-2"))
        runCurrent()
        fake.last("episodes").answer(episodes("2", 1, complete = false))
        fake.last("watch_series").answer(seriesWatch(null))
        runCurrent()
        flow.resume(fromStart = true)
        runCurrent()
        assertEquals(0, count("open_episode"))
        assertEquals("http_error", flow.state.value.episodes.ready().incompleteReason)
    }

    @Test
    fun `failed history reads leave search and episodes working`() = runTest {
        val flow = flow()
        flow.refreshRecent()
        runCurrent()
        fake.last("watch_recent").answer(failure("internal_error", "internal"))
        runCurrent()
        assertEquals("internal_error", (flow.state.value.recent as Load.Failed).code)

        flow.search("frieren")
        runCurrent()
        fake.last("search").answer(hits(hit("1")))
        runCurrent()
        flow.selectSeries(flow.state.value.search.ready().hits.single().ref())
        runCurrent()
        fake.last("watch_series").answer(failure("internal_error", "internal"))
        fake.last("episodes").answer(episodes("1", 1, 2))
        runCurrent()
        assertEquals(2, flow.state.value.episodes.ready().episodes.size)
        assertTrue(flow.state.value.watch is Load.Failed)
        assertFalse(flow.state.value.busy)

        flow.refreshWatch()
        runCurrent()
        fake.last("watch_series").answer(seriesWatch(null))
        runCurrent()
        assertNull(flow.state.value.watch.ready().lastOpened)
    }

    @Test
    fun `a watch_series reply for an earlier series never lands on the next one`() = runTest {
        val flow = flow()
        fake.ignoreCancel = true
        flow.selectSeries(SeriesRef("1", "slug-1", "Title 1", "https://shinden.pl/series/1-slug-1"))
        runCurrent()
        val stale = fake.last("watch_series")
        flow.selectSeries(SeriesRef("2", "slug-2", "Title 2", "https://shinden.pl/series/2-slug-2"))
        runCurrent()
        val fresh = fake.last("watch_series")

        stale.answer(seriesWatch(watch("1", 5, 1_000), watch("1", 5, 1_000)))
        runCurrent()
        assertEquals(Load.Loading, flow.state.value.watch)
        fresh.answer(seriesWatch(watch("2", 1, 2_000), watch("2", 1, 2_000)))
        runCurrent()
        assertEquals("2", flow.state.value.watch.ready().seriesId)
        assertEquals(1.0, flow.state.value.watch.ready().lastOpened!!.number, 0.0)
    }

    @Test
    fun `a write during a watch_series read drops the stale reply and reads again`() = runTest {
        val flow = flow()
        flow.selectSeries(SeriesRef("1", "slug-1", "Title 1", "https://shinden.pl/series/1-slug-1"))
        runCurrent()
        val first = fake.last("watch_series")
        flow.onWatchSaved(saved("1"))
        runCurrent()
        assertEquals("no second read while one is in flight", 1, count("watch_series"))

        first.answer(seriesWatch(watch("1", 3, 10_000), watch("1", 3, 10_000)))
        runCurrent()
        assertEquals(Load.Loading, flow.state.value.watch)
        assertEquals(2, count("watch_series"))
        fake.last("watch_series").answer(seriesWatch(watch("1", 3, 95_000), watch("1", 3, 95_000)))
        runCurrent()
        assertEquals(95_000L, flow.state.value.watch.ready().lastOpened!!.positionMs)
    }

    @Test
    fun `a write during a watch_recent read drops the stale reply and reads again`() = runTest {
        val flow = flow()
        flow.refreshRecent()
        runCurrent()
        val first = fake.last("watch_recent")
        flow.onWatchSaved(saved("1", ended = true))
        first.answer(recent(watch("1", 3, 10_000)))
        runCurrent()
        assertEquals(2, count("watch_recent"))
        fake.last("watch_recent").answer(recent(watch("1", 3, 1_440_000, 1_440_000, watched = true)))
        runCurrent()
        assertTrue(flow.state.value.recent.ready().single().last.watched)
    }

    @Test
    fun `quick repeated refreshes, a rotation or a return from Home make one read`() = runTest {
        val flow = flow()
        flow.refreshRecent()
        flow.refreshRecent()
        runCurrent()
        assertEquals(1, count("watch_recent"))
        fake.last("watch_recent").answer(recent(watch("1", 3)))
        runCurrent()

        flow.selectSeries(flow.state.value.recent.ready().single().series)
        runCurrent()
        flow.refreshWatch()
        flow.refreshWatch()
        runCurrent()
        assertEquals(1, count("watch_series"))
        fake.last("watch_series").answer(seriesWatch(null))
        runCurrent()

        flow.refreshWatch()
        runCurrent()
        assertEquals("a screen shown again after the read reads once more", 2, count("watch_series"))
        assertEquals("no playback is started by a refresh", 0, count("open_episode") + count("playback_begin"))
    }

    @Test
    fun `periodic saves and saves of another series do not read this series again`() = runTest {
        val flow = flow()
        opened(flow, null)
        flow.onWatchSaved(saved("1", periodic = true))
        flow.onWatchSaved(saved("2"))
        runCurrent()
        assertEquals(1, count("watch_series"))

        flow.onWatchSaved(saved("1"))
        runCurrent()
        assertEquals(2, count("watch_series"))
        assertEquals("1", fake.last("watch_series").args.getString("series_id"))
        assertEquals("the catalogue history is read when it is shown again", 1, count("watch_recent"))
    }

    @Test
    fun `back from a series reads the history and a save at the catalogue reads it again`() = runTest {
        val flow = flow()
        opened(flow, null)
        flow.back()
        runCurrent()
        assertEquals(Load.Idle, flow.state.value.watch)
        assertEquals(2, count("watch_recent"))
        fake.last("watch_recent").answer(recent(watch("1", 3)))
        runCurrent()

        flow.onWatchSaved(saved("1"))
        runCurrent()
        assertEquals(3, count("watch_recent"))
        assertTrue("no session is touched by history reads", fake.calls.none { it.method == "close_session" })
    }

    @Test
    fun `cancelling a listing drops its history read, cancelling sources keeps it`() = runTest {
        val flow = flow()
        flow.selectSeries(SeriesRef("1", "slug-1", "Title 1", "https://shinden.pl/series/1-slug-1"))
        runCurrent()
        val read = fake.last("watch_series")
        flow.cancel()
        runCurrent()
        assertTrue(fake.last("episodes").cancelled)
        assertTrue("the series was left", read.cancelled)
        assertNull(flow.state.value.series)

        flow.selectSeries(SeriesRef("1", "slug-1", "Title 1", "https://shinden.pl/series/1-slug-1"))
        runCurrent()
        fake.last("episodes").answer(episodes("1", 1))
        runCurrent()
        val kept = fake.last("watch_series")
        flow.openEpisode(flow.state.value.episodes.ready().episodes.single())
        runCurrent()
        flow.cancel()
        runCurrent()
        assertTrue(fake.last("open_episode").cancelled)
        assertFalse(kept.cancelled)
        kept.answer(seriesWatch(watch("1", 1, 5_000), watch("1", 1, 5_000)))
        runCurrent()
        assertEquals(5_000L, flow.state.value.watch.ready().lastOpened!!.positionMs)
    }

    @Test
    fun `a search neither cancels nor waits for the history`() = runTest {
        val flow = flow()
        flow.refreshRecent()
        runCurrent()
        val read = fake.last("watch_recent")
        flow.search("frieren")
        runCurrent()
        assertFalse(read.cancelled)
        fake.last("search").answer(hits(hit("1")))
        runCurrent()
        assertEquals(1, flow.state.value.search.ready().hits.size)

        read.answer(recent(watch("1", 3)))
        runCurrent()
        assertEquals(1, flow.state.value.recent.ready().size)
    }

    @Test
    fun `clearing the search returns to the history`() = runTest {
        val flow = flow()
        history(flow, watch("1", 3))
        flow.search("frieren")
        runCurrent()
        val pending = fake.last("search")
        flow.clearSearch()
        runCurrent()
        assertTrue(pending.cancelled)
        assertEquals(Load.Idle, flow.state.value.search)
        assertEquals(1, flow.state.value.recent.ready().size)
    }
}
