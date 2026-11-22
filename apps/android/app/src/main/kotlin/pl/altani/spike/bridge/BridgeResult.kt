package pl.altani.spike.bridge

import org.json.JSONException
import org.json.JSONObject

/** One call's outcome; `toString` never prints `data`, which may hold stream URLs and headers. */
sealed interface BridgeResult {
    class Ok(val data: JSONObject) : BridgeResult {
        override fun toString(): String = "Ok"
    }

    /** `kind` is one of the closed set in CONTRACT.md; the bridge's `message` is left out on purpose. */
    data class Failure(val code: String, val kind: String, val retryable: Boolean, val params: JSONObject) : BridgeResult

    /** Cancelled on the Kotlin side before Python was called. */
    data object NotStarted : BridgeResult

    companion object {
        fun parse(envelope: String): BridgeResult =
            try {
                val json = JSONObject(envelope)
                if (json.getBoolean("ok")) {
                    Ok(json.optJSONObject("data") ?: JSONObject())
                } else {
                    val error = json.getJSONObject("error")
                    Failure(
                        code = error.getString("code"),
                        kind = error.getString("kind"),
                        retryable = error.optBoolean("retryable"),
                        params = error.optJSONObject("params") ?: JSONObject(),
                    )
                }
            } catch (e: JSONException) {
                internal("malformed_envelope")
            }

        fun internal(reason: String): Failure = Failure("internal", "internal", false, JSONObject().put("reason", reason))
    }
}

val BridgeResult.isCancelled: Boolean
    get() = this is BridgeResult.NotStarted || (this is BridgeResult.Failure && kind == "cancelled")
