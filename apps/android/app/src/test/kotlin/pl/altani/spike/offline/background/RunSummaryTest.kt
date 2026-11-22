package pl.altani.spike.offline.background

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

fun job(
    state: String,
    reason: String? = null,
    done: Long = 0,
    total: Long? = null,
    notBefore: String? = null,
    cellular: Boolean = false,
): JSONObject =
    JSONObject()
        .put("state", state)
        .put("wait_reason", reason ?: JSONObject.NULL)
        .put("bytes_done", done)
        .put("bytes_total", total ?: JSONObject.NULL)
        .put("not_before", notBefore ?: JSONObject.NULL)
        .put("cellular_allowed", cellular)

fun snapshot(vararg jobs: JSONObject): JSONObject = JSONObject().put("queue_revision", 1).put("jobs", JSONArray(jobs.toList()))

fun preferences(network: String = "wifi_only"): JSONObject = JSONObject().put("download_network", network).put("revision", 1)

class RunSummaryTest {
    private val now = 1_800_000_000_000L
    private val past = "2027-01-15T08:00:00+00:00"
    private val future = "2027-01-15T09:00:00.250000+00:00"

    private fun summary(vararg jobs: JSONObject, network: String = "wifi_only") = RunSummary.of(snapshot(*jobs), preferences(network), now)

    @Test
    fun `every job state lands in exactly one bucket`() {
        val cases = mapOf(
            job("running") to RunPhase.WORKING,
            job("stopping") to RunPhase.WORKING,
            job("finalizing") to RunPhase.WORKING,
            job("queued") to RunPhase.WORKING,
            job("queued", notBefore = past) to RunPhase.WORKING,
            job("queued", notBefore = future) to RunPhase.DEFERRED,
            job("waiting", "network") to RunPhase.WAITING_NETWORK,
            job("waiting", "interaction") to RunPhase.NEEDS_USER,
            job("waiting", "source_decision") to RunPhase.NEEDS_USER,
            job("waiting", "storage") to RunPhase.NEEDS_USER,
            job("paused") to RunPhase.IDLE,
            job("failed") to RunPhase.IDLE,
            job("completed") to RunPhase.IDLE,
            job("cancelled") to RunPhase.IDLE,
        )
        for ((job, phase) in cases) assertEquals(job.toString(), phase, summary(job).phase)
    }

    @Test
    fun `one job that can run keeps the session working whatever else waits`() {
        val mixed = summary(job("waiting", "storage"), job("waiting", "network"), job("queued", notBefore = future), job("queued"))
        assertEquals(RunPhase.WORKING, mixed.phase)
        assertEquals(1, mixed.readyNow)
        assertEquals(0, mixed.activeIo)
        assertEquals(RunPhase.DEFERRED, summary(job("waiting", "network"), job("queued", notBefore = future)).phase)
        assertEquals(RunPhase.WAITING_NETWORK, summary(job("waiting", "interaction"), job("waiting", "network")).phase)
    }

    @Test
    fun `progress sums the transfers and an unknown size makes the total unknown`() {
        val known = summary(job("running", done = 100, total = 400), job("finalizing", done = 50, total = 50))
        assertEquals(150L, known.bytesDone)
        assertEquals(450L, known.bytesTotal)
        assertEquals(300L, known.remainingBytes)
        val unknown = summary(job("running", done = 100, total = 400), job("running", done = 7))
        assertEquals(107L, unknown.bytesDone)
        assertNull(unknown.bytesTotal)
        assertNull(unknown.remainingBytes)
        assertNull(summary(job("queued")).bytesTotal)
    }

    @Test
    fun `mobile data widens the scope through the policy or an open job's consent only`() {
        assertEquals(NetworkScope.WIFI, summary(job("queued")).scope)
        assertEquals(NetworkScope.ANY, summary(job("queued"), network = "any").scope)
        assertEquals(NetworkScope.ANY, summary(job("waiting", "network", cellular = true)).scope)
        assertEquals(NetworkScope.WIFI, summary(job("paused", cellular = true), job("queued")).scope)
    }

    @Test
    fun `an unreadable not_before counts as due`() {
        assertEquals(RunPhase.WORKING, summary(job("queued", notBefore = "tomorrow")).phase)
        assertEquals(RunPhase.WORKING, summary(job("queued", notBefore = "2027-01-15T09:00:00")).phase)
    }

    @Test
    fun `a narrower scheduled scope is covered by any desired one, a wider one only by any`() {
        assertEquals(true, NetworkScope.WIFI.coveredBy(NetworkScope.WIFI))
        assertEquals(true, NetworkScope.WIFI.coveredBy(NetworkScope.ANY))
        assertEquals(true, NetworkScope.ANY.coveredBy(NetworkScope.ANY))
        assertEquals(false, NetworkScope.ANY.coveredBy(NetworkScope.WIFI))
    }
}
