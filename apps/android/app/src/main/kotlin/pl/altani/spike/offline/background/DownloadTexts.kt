package pl.altani.spike.offline.background

import java.util.Locale

/** Notification text; kept in Kotlin like the rest of the app's UI text. */
object DownloadTexts {
    const val TITLE = "Offline downloads"
    const val ATTENTION_TITLE = "Downloads need your action"
    const val PREPARING = "Preparing…"
    const val RETRYING = "Waiting to retry"
    const val FINISHING = "Finishing…"
    const val STOP = "Stop"
    const val CHANNEL_NAME = "Offline downloads"

    fun working(active: Int, left: Int, done: Long, total: Long?): String {
        val amount = if (total != null) "${megabytes(done)} of ${megabytes(total)}" else "${megabytes(done)} downloaded"
        val head = if (active > 1) "$active downloads, $amount" else amount
        return if (left > 0) "$head · $left more queued" else head
    }

    fun waitingNetwork(scope: NetworkScope): String =
        if (scope == NetworkScope.WIFI) "Waiting for Wi-Fi" else "Waiting for a network connection"

    fun attention(reason: AttentionReason): String =
        when (reason) {
            AttentionReason.NEEDS_USER -> "A download is waiting for your decision. Open the app to continue."
            AttentionReason.NETWORK_WAIT -> "Downloads stopped while waiting for the network. Resume them in the app."
            AttentionReason.WINDOW_EXPIRED -> "Downloads were not resumed automatically. Resume them in the app."
            AttentionReason.START_FAILED -> "Downloads could not start. Open the app to retry."
            AttentionReason.STOPPED -> "Downloads stopped unexpectedly. Resume them in the app."
        }

    private fun megabytes(bytes: Long): String = String.format(Locale.ROOT, "%.1f MB", bytes / 1_048_576.0)
}
