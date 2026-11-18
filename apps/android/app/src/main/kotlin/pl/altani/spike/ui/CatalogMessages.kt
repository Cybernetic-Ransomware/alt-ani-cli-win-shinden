package pl.altani.spike.ui

/** User-facing text for a bridge or controller code; built from the code alone, never from `params` or a message. */
object CatalogMessages {
    private val ACCESS = setOf("access_blocked", "xhr_access_blocked", "interaction.required")
    private val NETWORK = setOf(
        "network.offline", "timeout", "network_error", "transport_unavailable", "http_status", "xhr_service_unavailable",
    )
    private val NO_SOURCE = setOf(
        "player_not_found", "no_stream", "sources_exhausted", "unsupported_host", "java_script_required", "xhr_next_player",
    )
    private val SITE_CHANGED = setOf("parse", "xhr_api_protocol_error", "xhr_players_changed", "malformed_reply")

    fun describe(code: String, kind: String? = null): String =
        when {
            code in ACCESS -> "Shinden blocked access (anti-bot protection). Try again later. ($code)"
            code == "network.offline" -> "No network connection. Connect and retry."
            code in NETWORK -> "Shinden did not answer properly. Check the connection and retry. ($code)"
            code in NO_SOURCE -> "No working player for this episode. ($code)"
            code == "criteria.no_match" -> "No player matches the audio, subtitle or quality preferences."
            code == CONSENT -> "Mobile data needs your consent for this episode."
            code in SITE_CHANGED -> "Shinden answered in an unexpected form. ($code)"
            code == "refresh_limit" -> "The stream kept expiring. Retry the episode."
            code.startsWith("player:") -> "The player could not play this stream. (${code.removePrefix("player:")})"
            code == "cancelled" -> "Cancelled."
            kind == "access" -> "Shinden blocked access. ($code)"
            kind == "unavailable" -> "Shinden is unavailable right now. ($code)"
            else -> "Unexpected error. ($code)"
        }

    private const val CONSENT = "network.cellular_consent_required"
}
