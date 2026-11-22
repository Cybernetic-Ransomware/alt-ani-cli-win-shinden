package pl.altani.spike.offline.background

sealed interface NotificationProgress {
    data class Determinate(val max: Int, val value: Int) : NotificationProgress

    data object Indeterminate : NotificationProgress
}

/** What the session notification shows; built from a [RunSummary] only, so no title, URL or host ever reaches it. */
data class NotificationModel(val title: String, val text: String, val progress: NotificationProgress?)

enum class AttentionReason { NEEDS_USER, NETWORK_WAIT, WINDOW_EXPIRED, START_FAILED, STOPPED }

object NotificationModels {
    const val PROGRESS_MAX = 1000

    val preparing = NotificationModel(DownloadTexts.TITLE, DownloadTexts.PREPARING, NotificationProgress.Indeterminate)

    fun session(summary: RunSummary): NotificationModel =
        when (summary.phase) {
            RunPhase.WORKING -> {
                val total = summary.bytesTotal
                val progress = if (summary.activeIo == 1 && total != null && total > 0) {
                    NotificationProgress.Determinate(PROGRESS_MAX, (summary.bytesDone * PROGRESS_MAX / total).toInt().coerceIn(0, PROGRESS_MAX))
                } else {
                    NotificationProgress.Indeterminate
                }
                val left = summary.readyNow + summary.deferred + summary.waitingNetwork
                NotificationModel(DownloadTexts.TITLE, DownloadTexts.working(summary.activeIo, left, summary.bytesDone, total), progress)
            }
            RunPhase.DEFERRED -> NotificationModel(DownloadTexts.TITLE, DownloadTexts.RETRYING, null)
            RunPhase.WAITING_NETWORK ->
                NotificationModel(DownloadTexts.TITLE, DownloadTexts.waitingNetwork(summary.scope), null)
            RunPhase.NEEDS_USER, RunPhase.IDLE -> NotificationModel(DownloadTexts.TITLE, DownloadTexts.FINISHING, null)
        }

    fun attention(reason: AttentionReason): NotificationModel =
        NotificationModel(DownloadTexts.ATTENTION_TITLE, DownloadTexts.attention(reason), null)
}
