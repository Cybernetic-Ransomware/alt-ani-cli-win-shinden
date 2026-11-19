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

        fun failure(code: String, kind: String = "unavailable") = BridgeResult.Failure(code, kind, true, JSONObject())
    }
}
