package pl.altani.spike.playback

import android.content.Context
import android.net.Uri
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.HttpDataSource
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.source.MediaSource
import androidx.media3.exoplayer.upstream.DefaultLoadErrorHandlingPolicy
import androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy
import java.io.File

/** Response codes that mean the signed URL is no longer valid; only `stream_refresh` can fix them. */
val EXPIRED_URL_CODES = setOf(401, 403, 410)

fun expiredUrlCode(error: Throwable?): Int? {
    var cause = error
    while (cause != null) {
        if (cause is HttpDataSource.InvalidResponseCodeException && cause.responseCode in EXPIRED_URL_CODES) {
            return cause.responseCode
        }
        cause = cause.cause
    }
    return null
}

/** Retrying an expired URL only repeats the 403, so it surfaces at once and the client refreshes instead. */
@UnstableApi
class ExpiredUrlPolicy : DefaultLoadErrorHandlingPolicy() {
    override fun getRetryDelayMsFor(loadErrorInfo: LoadErrorHandlingPolicy.LoadErrorInfo): Long =
        if (expiredUrlCode(loadErrorInfo.exception) != null) C.TIME_UNSET else super.getRetryDelayMsFor(loadErrorInfo)
}

@UnstableApi
object MediaSources {
    fun create(context: Context, source: PlaybackSource, hold: TransferHold = TransferHold.None): MediaSource =
        when (source) {
            is PlaybackSource.Online -> online(context, source.variant, hold)
            is PlaybackSource.Local -> local(context, source)
        }

    private fun online(context: Context, variant: Variant, hold: TransferHold): MediaSource {
        // one factory per variant: its headers go on every request, playlists and segments included
        val http = DefaultHttpDataSource.Factory()
            .setDefaultRequestProperties(variant.headers.filterKeys { !it.equals("User-Agent", ignoreCase = true) })
            .setAllowCrossProtocolRedirects(false)
        variant.headers.entries.firstOrNull { it.key.equals("User-Agent", ignoreCase = true) }?.let { http.setUserAgent(it.value) }
        val item = MediaItem.Builder().setUri(variant.url).setMimeType(mimeOf(variant)).build()
        return DefaultMediaSourceFactory(DefaultDataSource.Factory(context, hold.upstream(http)))
            .setLoadErrorHandlingPolicy(hold.errorPolicy(ExpiredUrlPolicy()))
            .createMediaSource(item)
    }

    private fun local(context: Context, source: PlaybackSource.Local): MediaSource {
        val uri = if (source.kind == "file") Uri.fromFile(File(source.value)) else Uri.parse(source.value)
        return DefaultMediaSourceFactory(DefaultDataSource.Factory(context)).createMediaSource(MediaItem.fromUri(uri))
    }

    private fun mimeOf(variant: Variant): String? =
        when (variant.protocol) {
            "hls" -> MimeTypes.APPLICATION_M3U8
            "dash" -> MimeTypes.APPLICATION_MPD
            else -> variant.mime
        }
}
