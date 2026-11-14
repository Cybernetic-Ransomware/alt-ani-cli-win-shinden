package pl.altani.spike.playback

import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.exoplayer.LoadControl
import androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy

/** How the player is held while the [TransferGate] is closed; applies to network sources only. */
@UnstableApi
interface TransferHold {
    fun loadControl(base: LoadControl): LoadControl = base

    fun upstream(http: DataSource.Factory): DataSource.Factory = http

    fun errorPolicy(base: LoadErrorHandlingPolicy): LoadErrorHandlingPolicy = base

    object None : TransferHold
}
