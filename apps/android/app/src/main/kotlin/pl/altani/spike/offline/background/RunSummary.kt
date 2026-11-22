package pl.altani.spike.offline.background

import java.time.OffsetDateTime
import java.time.format.DateTimeParseException
import org.json.JSONObject

/** The order is the precedence: a session is working while anything can run, whatever else waits. */
enum class RunPhase { WORKING, DEFERRED, WAITING_NETWORK, NEEDS_USER, IDLE }

/** Networks the open jobs may use; [WIFI] matches the engine's `wifi_only` (Wi-Fi or Ethernet transport). */
enum class NetworkScope {
    WIFI,
    ANY,
    ;

    /** A system restart with this scope only makes sense when [desired] accepts at least the same networks. */
    fun coveredBy(desired: NetworkScope): Boolean = this == WIFI || desired == ANY
}

data class RunSummary(
    val activeIo: Int = 0,
    val readyNow: Int = 0,
    val deferred: Int = 0,
    val waitingNetwork: Int = 0,
    val needsUser: Int = 0,
    val bytesDone: Long = 0,
    /** Null when no transfer runs or one of them does not know its size. */
    val bytesTotal: Long? = null,
    val scope: NetworkScope = NetworkScope.WIFI,
) {
    val phase: RunPhase
        get() = when {
            activeIo > 0 || readyNow > 0 -> RunPhase.WORKING
            deferred > 0 -> RunPhase.DEFERRED
            waitingNetwork > 0 -> RunPhase.WAITING_NETWORK
            needsUser > 0 -> RunPhase.NEEDS_USER
            else -> RunPhase.IDLE
        }

    val remainingBytes: Long?
        get() = bytesTotal?.let { (it - bytesDone).coerceAtLeast(0) }

    companion object {
        /** `queue_snapshot`'s `states`: completed and cancelled jobs never keep a session. */
        val OPEN_STATES = listOf("queued", "running", "stopping", "finalizing", "paused", "waiting", "failed")

        private val ACTIVE = setOf("running", "stopping", "finalizing")
        private val USER_WAITS = setOf("interaction", "source_decision", "storage")

        /** [nowEpochMs] is wall time, the clock the engine writes `not_before` with. */
        fun of(snapshot: JSONObject, preferences: JSONObject, nowEpochMs: Long): RunSummary {
            val jobs = snapshot.getJSONArray("jobs")
            var activeIo = 0
            var readyNow = 0
            var deferred = 0
            var waitingNetwork = 0
            var needsUser = 0
            var bytesDone = 0L
            var bytesTotal: Long? = 0L
            var cellular = preferences.optString("download_network") == "any"
            for (i in 0 until jobs.length()) {
                val job = jobs.getJSONObject(i)
                val state = job.getString("state")
                val reason = if (job.isNull("wait_reason")) null else job.getString("wait_reason")
                val open = when {
                    state in ACTIVE -> {
                        activeIo++
                        bytesDone += job.optLong("bytes_done", 0)
                        bytesTotal = if (job.isNull("bytes_total")) null else bytesTotal?.plus(job.getLong("bytes_total"))
                        true
                    }
                    state == "queued" -> {
                        if (dueLater(job, nowEpochMs)) deferred++ else readyNow++
                        true
                    }
                    state == "waiting" && reason == "network" -> {
                        waitingNetwork++
                        true
                    }
                    state == "waiting" && reason in USER_WAITS -> {
                        needsUser++
                        false
                    }
                    else -> false
                }
                if (open && job.optBoolean("cellular_allowed")) cellular = true
            }
            return RunSummary(
                activeIo = activeIo,
                readyNow = readyNow,
                deferred = deferred,
                waitingNetwork = waitingNetwork,
                needsUser = needsUser,
                bytesDone = bytesDone,
                bytesTotal = if (activeIo == 0) null else bytesTotal,
                scope = if (cellular) NetworkScope.ANY else NetworkScope.WIFI,
            )
        }

        /** An unreadable `not_before` counts as due: the engine decides, and keeping the session is the safe side. */
        private fun dueLater(job: JSONObject, nowEpochMs: Long): Boolean {
            if (job.isNull("not_before")) return false
            return try {
                OffsetDateTime.parse(job.getString("not_before")).toInstant().toEpochMilli() > nowEpochMs
            } catch (e: DateTimeParseException) {
                false
            }
        }
    }
}
