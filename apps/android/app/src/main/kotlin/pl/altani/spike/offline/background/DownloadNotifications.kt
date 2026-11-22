package pl.altani.spike.offline.background

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat

class DownloadNotifications(private val context: Context) : AttentionNotifier {
    private val manager = context.getSystemService(NotificationManager::class.java)

    fun createChannel() {
        // offline downloads need API 30 (ADR-0007); below it nothing posts to the channel
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
        val channel = NotificationChannel(CHANNEL_ID, DownloadTexts.CHANNEL_NAME, NotificationManager.IMPORTANCE_LOW)
        channel.setShowBadge(false)
        manager.createNotificationChannel(channel)
    }

    /** [generation] null: before the session exists, so no Stop action can point at it yet. */
    fun session(model: NotificationModel, generation: Long?): Notification {
        val builder = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(model.title)
            .setContentText(model.text)
            .setContentIntent(openApp())
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
        when (val progress = model.progress) {
            is NotificationProgress.Determinate -> builder.setProgress(progress.max, progress.value, false)
            NotificationProgress.Indeterminate -> builder.setProgress(0, 0, true)
            null -> Unit
        }
        if (generation != null) {
            val stop = Intent(context, RunStopReceiver::class.java)
                .setAction(RunStopReceiver.ACTION_STOP)
                .putExtra(RunStopReceiver.EXTRA_GENERATION, generation)
            val pending = PendingIntent.getBroadcast(context, 0, stop, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            builder.addAction(0, DownloadTexts.STOP, pending)
        }
        return builder.build()
    }

    /** Without `POST_NOTIFICATIONS` the system drops it silently; the work itself never depends on it. */
    fun showSession(model: NotificationModel, generation: Long): Notification =
        session(model, generation).also { manager.notify(NOTIFICATION_RUN, it) }

    override fun post(reason: AttentionReason) {
        val model = NotificationModels.attention(reason)
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_warning)
            .setContentTitle(model.title)
            .setContentText(model.text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(model.text))
            .setContentIntent(openApp())
            .setAutoCancel(true)
            .build()
        manager.notify(NOTIFICATION_ATTENTION, notification)
    }

    override fun clear() = manager.cancel(NOTIFICATION_ATTENTION)

    private fun openApp(): PendingIntent {
        // the launcher entry brings back the existing task instead of stacking a second Activity
        val intent = context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?: Intent().setPackage(context.packageName)
        return PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    companion object {
        const val CHANNEL_ID = "offline_downloads"
        const val NOTIFICATION_RUN = 52_001
        const val NOTIFICATION_ATTENTION = 52_002
    }
}
