package pl.altani.spike.ui

import kotlin.coroutines.Continuation
import kotlin.coroutines.cancellation.CancellationException
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine
import kotlinx.coroutines.CompletableDeferred
import org.json.JSONArray
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult

/** Bridge calls held until a test answers them; [ignoreCancel] models a reply already on its way when the caller cancels. */
class FakeCalls {
    inner class Pending(val method: String, val args: JSONObject) {
        internal val reply = CompletableDeferred<BridgeResult>()
        internal var continuation: Continuation<BridgeResult>? = null
        var cancelled = false

        fun answer(result: BridgeResult) {
            continuation?.resume(result) ?: reply.complete(result)
        }
    }

    val calls = mutableListOf<Pending>()
    var ignoreCancel = false

    suspend fun call(method: String, args: JSONObject): BridgeResult {
        val pending = Pending(method, args).also { calls += it }
        if (ignoreCancel) return suspendCoroutine { pending.continuation = it }
        return try {
            pending.reply.await()
        } catch (e: CancellationException) {
            pending.cancelled = true
            throw e
        }
    }

    fun last(method: String): Pending = calls.last { it.method == method }

    companion object {
        fun hit(id: String, title: String = "Title $id") =
            JSONObject().put("id", id).put("slug", "slug-$id").put("title", title).put("url", "https://shinden.pl/series/$id-slug-$id")
                .put("series_type", "TV")

        fun hits(vararg hits: JSONObject, complete: Boolean = true, total: Int? = hits.size) =
            BridgeResult.Ok(
                JSONObject()
                    .put("hits", JSONArray(hits.toList()))
                    .put("total", total ?: JSONObject.NULL)
                    .put("complete", complete)
                    .put("incomplete_reason", if (complete) JSONObject.NULL else "page_limit"),
            )

        fun episodes(seriesId: String, vararg numbers: Number, complete: Boolean = true) =
            BridgeResult.Ok(
                JSONObject()
                    .put("series", hit(seriesId).apply { remove("series_type") })
                    .put(
                        "episodes",
                        JSONArray(numbers.map { JSONObject().put("number", it).put("title", "Ep $it").put("url", "https://shinden.pl/episode/$seriesId/view/$it") }),
                    )
                    .put("complete", complete)
                    .put("incomplete_reason", if (complete) JSONObject.NULL else "http_error"),
            )

        fun sources(sessionId: String, vararg ids: String) =
            BridgeResult.Ok(
                JSONObject()
                    .put("session_id", sessionId)
                    .put("episode", JSONObject())
                    .put(
                        "sources",
                        JSONArray(
                            ids.map {
                                JSONObject().put("online_id", it).put("player", "cda").put("lang_audio", "jp").put("lang_subs", "pl")
                                    .put("max_res", "1080p").put("date_added", JSONObject.NULL).put("subs_author", "Example Subs")
                                    .put("source", "https://subs.example/")
                            },
                        ),
                    ),
            )

        fun airDates(vararg dates: List<Int>?) =
            BridgeResult.Ok(
                JSONObject().put(
                    "metadata",
                    JSONArray(dates.map { date -> date?.let { JSONObject().put("air_date_sort", JSONArray(it)).put("description", "") } ?: JSONObject.NULL }),
                ),
            )

        fun failure(code: String, kind: String = "unavailable") = BridgeResult.Failure(code, kind, true, JSONObject())

        /** A `watch` DTO; [opened] null means the episode has a row but was never opened. */
        fun watch(
            seriesId: String,
            number: Number,
            position: Long? = null,
            duration: Long? = null,
            watched: Boolean = false,
            opened: Double? = 1_700_000_000.0,
            revision: Int = 1,
        ): JSONObject =
            JSONObject()
                .put("series_id", seriesId)
                .put("number", number.toDouble())
                .put("revision", revision)
                .put("last_opened_at", opened ?: JSONObject.NULL)
                .put("opened_via", if (opened != null) "player" else JSONObject.NULL)
                .put("position_ms", position ?: JSONObject.NULL)
                .put("duration_ms", duration ?: JSONObject.NULL)
                .put("position_at", JSONObject.NULL)
                .put("watched", watched)
                .put("watched_source", if (watched) "playback_ended" else JSONObject.NULL)
                .put("watched_at", JSONObject.NULL)

        fun recent(vararg rows: JSONObject) =
            BridgeResult.Ok(
                JSONObject().put(
                    "series",
                    JSONArray(rows.map { JSONObject().put("series", hit(it.getString("series_id")).apply { remove("series_type") }).put("watch", it) }),
                ),
            )

        /** A `preferences` DTO; the defaults are the engine's. */
        fun preferences(
            revision: Long = 0,
            audio: String = "any",
            subs: String = "any",
            quality: String = "best",
            network: String = "wifi_only",
            alternatives: Boolean = false,
            active: Int = 1,
        ): JSONObject =
            JSONObject()
                .put("audio", audio)
                .put("subs", subs)
                .put("quality", quality)
                .put("download_network", network)
                .put("auto_alternatives", alternatives)
                .put("max_active_downloads", active)
                .put("revision", revision)

        fun seriesWatch(last: JSONObject?, vararg episodes: JSONObject) =
            BridgeResult.Ok(JSONObject().put("last_opened", last ?: JSONObject.NULL).put("episodes", JSONArray(episodes.toList())))
    }
}

/** `keySet()` is missing from the Android stubs the unit tests compile against. */
fun JSONObject.keyNames(): Set<String> = keys().asSequence().toSet()
