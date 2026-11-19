package pl.altani.spike.ui

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.ui.CatalogFlow.Load
import pl.altani.spike.ui.FakeCalls.Companion.episodes
import pl.altani.spike.ui.FakeCalls.Companion.failure
import pl.altani.spike.ui.FakeCalls.Companion.hit
import pl.altani.spike.ui.FakeCalls.Companion.hits
import pl.altani.spike.ui.FakeCalls.Companion.sources

@OptIn(ExperimentalCoroutinesApi::class)
class CatalogFlowTest {
    private val fake = FakeCalls()

    private fun TestScope.flow() = CatalogFlow(backgroundScope, fake::call, clock = { 0L })

    private fun <T> Load<T>.ready(): T = (this as Load.Ready<T>).value

    private fun TestScope.listed(flow: CatalogFlow, vararg ids: String) {
        flow.search("frieren")
        runCurrent()
        fake.last("search").answer(hits(*ids.map { hit(it) }.toTypedArray()))
        runCurrent()
    }

    @Test
    fun `search maps hits to immutable models`() = runTest {
        val flow = flow()
        flow.search("  frieren ")
        runCurrent()
        assertEquals(Load.Loading, flow.state.value.search)
        assertEquals("frieren", fake.last("search").args.getString("query"))

        fake.last("search").answer(hits(hit("1", "Frieren")))
        runCurrent()

        val page = flow.state.value.search.ready()
        assertEquals(listOf(SeriesHit("1", "slug-1", "Frieren", "https://shinden.pl/series/1-slug-1", "TV")), page.hits)
        assertNull(page.incompleteReason)
    }

    @Test
    fun `an empty or partial search is ready, not failed`() = runTest {
        val flow = flow()
        flow.search("nothing")
        runCurrent()
        fake.last("search").answer(hits())
        runCurrent()
        assertTrue(flow.state.value.search.ready().hits.isEmpty())

        flow.search("many")
        runCurrent()
        fake.last("search").answer(hits(hit("1"), complete = false, total = 300))
        runCurrent()
        val page = flow.state.value.search.ready()
        assertEquals("page_limit", page.incompleteReason)
        assertEquals(300, page.total)
    }

    @Test
    fun `choosing a series sends its reference and lists its episodes`() = runTest {
        val flow = flow()
        listed(flow, "1")
        val chosen = flow.state.value.search.ready().hits.single()

        flow.selectSeries(chosen)
        runCurrent()
        val sent = fake.last("episodes").args.getJSONObject("series")
        assertEquals(setOf("id", "slug", "title", "url"), sent.keys().asSequence().toSet())
        fake.last("episodes").answer(episodes("1", 2, 1, 12.5))
        runCurrent()

        val page = flow.state.value.episodes.ready()
        assertEquals(listOf("2", "1", "12.5"), page.episodes.map { it.label })
        assertEquals(1L, page.episodes[1].toJson().get("number"))
    }

    @Test
    fun `another series cancels the pending listing and its late reply is dropped`() = runTest {
        val flow = flow()
        listed(flow, "1", "2")
        val (first, second) = flow.state.value.search.ready().hits

        flow.selectSeries(first)
        runCurrent()
        val stale = fake.last("episodes")
        flow.selectSeries(second)
        runCurrent()
        assertTrue("the first call was cancelled", stale.cancelled)

        fake.last("episodes").answer(episodes("2", 1))
        runCurrent()
        assertEquals(second, flow.state.value.series)
        assertEquals("2", flow.state.value.episodes.ready().series.id)
    }

    @Test
    fun `a reply that arrives despite its cancellation never replaces the newer choice`() = runTest {
        val flow = flow()
        listed(flow, "1", "2")
        val (first, second) = flow.state.value.search.ready().hits
        fake.ignoreCancel = true

        flow.selectSeries(first)
        runCurrent()
        val stale = fake.last("episodes")
        flow.selectSeries(second)
        runCurrent()
        val fresh = fake.last("episodes")

        fresh.answer(episodes("2", 1))
        runCurrent()
        stale.answer(episodes("1", 1, 2, 3))
        runCurrent()

        assertEquals(second, flow.state.value.series)
        assertEquals("2", flow.state.value.episodes.ready().series.id)
    }

    @Test
    fun `a new search cancels the listing and clears the choice`() = runTest {
        val flow = flow()
        listed(flow, "1")
        flow.selectSeries(flow.state.value.search.ready().hits.single())
        runCurrent()
        val pending = fake.last("episodes")

        flow.search("other")
        runCurrent()

        assertTrue(pending.cancelled)
        assertNull(flow.state.value.series)
        assertEquals(Load.Idle, flow.state.value.episodes)
        assertEquals(Load.Loading, flow.state.value.search)
    }

    @Test
    fun `cancel stops the running call`() = runTest {
        val flow = flow()
        flow.search("frieren")
        runCurrent()
        flow.cancel()
        runCurrent()
        assertTrue(fake.last("search").cancelled)
        assertEquals(Load.Idle, flow.state.value.search)
        assertFalse(flow.state.value.busy)

        listed(flow, "1")
        flow.selectSeries(flow.state.value.search.ready().hits.single())
        runCurrent()
        flow.cancel()
        runCurrent()
        assertTrue(fake.last("episodes").cancelled)
        assertNull(flow.state.value.series)
        assertEquals(1, flow.state.value.search.ready().hits.size)
    }

    @Test
    fun `a failure is presented and retry repeats the same call`() = runTest {
        val flow = flow()
        listed(flow, "1")
        val chosen = flow.state.value.search.ready().hits.single()
        flow.selectSeries(chosen)
        runCurrent()
        fake.last("episodes").answer(BridgeResult.Failure("access_blocked", "access", false, JSONObject().put("block", "cloudflare")))
        runCurrent()

        val failed = flow.state.value.episodes as Load.Failed
        assertEquals("access_blocked", failed.code)
        assertEquals(CatalogMessages.describe("access_blocked", "access"), failed.message)

        flow.retry()
        runCurrent()
        assertEquals(2, fake.calls.count { it.method == "episodes" })
        assertEquals(chosen.id, fake.last("episodes").args.getJSONObject("series").getString("id"))
        fake.last("episodes").answer(episodes("1", 1))
        runCurrent()
        assertEquals(1, flow.state.value.episodes.ready().episodes.size)
    }

    @Test
    fun `retry after a failed search searches again`() = runTest {
        val flow = flow()
        flow.search("frieren")
        runCurrent()
        fake.last("search").answer(failure("network.offline"))
        runCurrent()
        assertEquals("network.offline", (flow.state.value.search as Load.Failed).code)

        flow.retry()
        runCurrent()
        assertEquals(listOf("frieren", "frieren"), fake.calls.map { it.args.getString("query") })
    }

    @Test
    fun `an incomplete listing is kept with its reason and a malformed one fails`() = runTest {
        val flow = flow()
        listed(flow, "1", "2")
        val (first, second) = flow.state.value.search.ready().hits
        flow.selectSeries(first)
        runCurrent()
        fake.last("episodes").answer(episodes("1", 1, 2, complete = false))
        runCurrent()
        assertEquals("http_error", flow.state.value.episodes.ready().incompleteReason)

        flow.selectSeries(second)
        runCurrent()
        fake.last("episodes").answer(BridgeResult.Ok(JSONObject().put("episodes", "nope")))
        runCurrent()
        assertEquals("malformed_reply", (flow.state.value.episodes as Load.Failed).code)
    }

    @Test
    fun `an episode from another listing is not played`() = runTest {
        val flow = flow()
        listed(flow, "1")
        flow.selectSeries(flow.state.value.search.ready().hits.single())
        runCurrent()
        fake.last("episodes").answer(episodes("1", 1))
        runCurrent()

        flow.openEpisode(Episode(7.0, "Ep 7", "https://shinden.pl/episode/9/view/7"))
        runCurrent()
        assertNull(flow.state.value.episode)
        assertEquals(0, fake.calls.count { it.method == "open_episode" })
    }

    private fun TestScope.episodeListed(flow: CatalogFlow, vararg numbers: Number) {
        listed(flow, "1")
        flow.selectSeries(flow.state.value.search.ready().hits.single())
        runCurrent()
        fake.last("episodes").answer(episodes("1", *numbers))
        runCurrent()
    }

    private fun TestScope.opened(flow: CatalogFlow, index: Int, session: String, vararg ids: String) {
        flow.openEpisode(flow.state.value.episodes.ready().episodes[index])
        runCurrent()
        fake.last("open_episode").answer(sources(session, *ids))
        runCurrent()
    }

    private fun closed(): List<String> = fake.calls.filter { it.method == "close_session" }.map { it.args.getString("session_id") }

    @Test
    fun `an episode lists its sources and a pick carries the session to the player`() = runTest {
        val flow = flow()
        episodeListed(flow, 1, 2)
        flow.openEpisode(flow.state.value.episodes.ready().episodes[0])
        runCurrent()
        assertEquals(Load.Loading, flow.state.value.sources)
        assertEquals(1L, fake.last("open_episode").args.getJSONObject("episode").get("number"))
        fake.last("open_episode").answer(sources("s1", "11", "12"))
        runCurrent()

        val page = flow.state.value.sources.ready()
        assertEquals(listOf("11", "12"), page.sources.map { it.onlineId })
        assertEquals("cda · audio jp · subs pl · 1080p · Example Subs", page.sources[0].label)

        val chosen = flow.pickSource(page.sources[1])!!
        assertEquals(PlaybackPick(flow.state.value.episodes.ready().series, page.episode, "s1", "12"), chosen)
        assertEquals("12", flow.state.value.source)
        val auto = flow.pickSource(null)!!
        assertNull(auto.onlineId)
        assertEquals("s1", auto.sessionId)
        assertEquals(CatalogFlow.AUTO, flow.state.value.source)
        assertTrue("the played session stays open", closed().isEmpty())
    }

    @Test
    fun `a session nobody uses is closed, the played one is kept`() = runTest {
        val flow = flow()
        episodeListed(flow, 1, 2, 3)
        opened(flow, 0, "s1", "11")
        flow.pickSource(null)
        opened(flow, 1, "s2", "21")
        assertTrue("s1 is still playing", closed().isEmpty())

        opened(flow, 2, "s3", "31")
        assertEquals(listOf("s2"), closed())

        flow.pickSource(null)
        runCurrent()
        assertEquals("s1 left the player", listOf("s2", "s1"), closed())

        flow.back()
        runCurrent()
        assertEquals("s3 still plays after Back", listOf("s2", "s1"), closed())
    }

    @Test
    fun `a sources reply that lost the race is closed and never shown`() = runTest {
        val flow = flow()
        episodeListed(flow, 1, 2)
        fake.ignoreCancel = true
        val (first, second) = flow.state.value.episodes.ready().episodes
        flow.openEpisode(first)
        runCurrent()
        val stale = fake.last("open_episode")
        flow.openEpisode(second)
        runCurrent()
        val fresh = fake.last("open_episode")

        stale.answer(sources("old", "1"))
        runCurrent()
        fresh.answer(sources("new", "2"))
        runCurrent()

        assertEquals("new", flow.state.value.sources.ready().sessionId)
        assertEquals(second, flow.state.value.episode)
        assertEquals(listOf("old"), closed())
    }

    @Test
    fun `a session closed by the bridge is reopened once, other sessions are ignored`() = runTest {
        val flow = flow()
        episodeListed(flow, 1)
        opened(flow, 0, "s1", "11")

        flow.onSessionClosed("unrelated")
        runCurrent()
        assertEquals(1, fake.calls.count { it.method == "open_episode" })

        flow.onSessionClosed("s1")
        runCurrent()
        assertEquals(Load.Loading, flow.state.value.sources)
        assertEquals(2, fake.calls.count { it.method == "open_episode" })
        assertTrue("the bridge already closed it", closed().isEmpty())
        fake.last("open_episode").answer(sources("s2", "11"))
        runCurrent()
        assertEquals("s2", flow.state.value.sources.ready().sessionId)
    }

    @Test
    fun `back from sources returns to episodes, cancel and retry work on the sources call`() = runTest {
        val flow = flow()
        episodeListed(flow, 1)
        val episode = flow.state.value.episodes.ready().episodes.single()
        flow.openEpisode(episode)
        runCurrent()
        flow.cancel()
        runCurrent()
        assertTrue(fake.last("open_episode").cancelled)
        assertNull(flow.state.value.episode)
        assertTrue(flow.state.value.series != null)

        flow.openEpisode(episode)
        runCurrent()
        fake.last("open_episode").answer(failure("xhr_access_blocked"))
        runCurrent()
        assertEquals("xhr_access_blocked", (flow.state.value.sources as Load.Failed).code)
        flow.retry()
        runCurrent()
        assertEquals(3, fake.calls.count { it.method == "open_episode" })

        fake.last("open_episode").answer(sources("s1", "11"))
        runCurrent()
        flow.back()
        runCurrent()
        assertEquals(Load.Idle, flow.state.value.sources)
        assertTrue(flow.state.value.episodes is Load.Ready)
        assertEquals(listOf("s1"), closed())
    }
}
