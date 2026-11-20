package pl.altani.spike.playback

import org.json.JSONObject

/** URLs and headers are short-lived secrets: memory only, never in a Bundle, a file or a log line. */
class Variant(
    val url: String,
    val protocol: String,
    val mime: String?,
    val label: String,
    val headers: Map<String, String>,
) {
    override fun toString(): String = "Variant($label, $protocol)"
}

/** What `playback_prepare` or `stream_refresh` chose; the client never picks a source or a variant itself. */
sealed interface PlaybackSource {
    class Online(val sessionId: String, val variant: Variant, val variantChanged: Boolean) : PlaybackSource {
        override fun toString(): String = "Online($variant, changed=$variantChanged)"
    }

    /** [kind] is `file` (a path) or `content_uri`. */
    class Local(val kind: String, val value: String) : PlaybackSource {
        override fun toString(): String = "Local($kind)"
    }

    companion object {
        fun parse(data: JSONObject): PlaybackSource {
            if (data.optString("mode") == "local") {
                val target = data.getJSONObject("target")
                return Local(target.getString("kind"), target.getString("value"))
            }
            val variant = data.getJSONObject("variant")
            val headers = variant.optJSONObject("headers")
            return Online(
                sessionId = data.getString("session_id"),
                variant = Variant(
                    url = variant.getString("url"),
                    protocol = variant.getString("protocol"),
                    mime = variant.optString("mime").takeIf { it.isNotEmpty() && !variant.isNull("mime") },
                    label = variant.optString("label"),
                    headers = headers?.keys()?.asSequence()?.associateWith { headers.getString(it) }.orEmpty(),
                ),
                variantChanged = data.optBoolean("variant_changed"),
            )
        }
    }
}
