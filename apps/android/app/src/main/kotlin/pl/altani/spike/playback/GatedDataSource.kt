package pl.altani.spike.playback

import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener

@UnstableApi
open class ForwardingDataSource(protected val upstream: DataSource) : DataSource {
    override fun addTransferListener(transferListener: TransferListener) = upstream.addTransferListener(transferListener)

    override fun open(dataSpec: DataSpec): Long = upstream.open(dataSpec)

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int = upstream.read(buffer, offset, length)

    override fun getUri(): Uri? = upstream.uri

    override fun getResponseHeaders(): Map<String, List<String>> = upstream.responseHeaders

    override fun close() = upstream.close()
}

/** T9 candidate K3: `open()` waits for the gate, so the loader neither retries nor looks idle to Media3. */
@UnstableApi
class GatedDataSource(upstream: DataSource, private val gate: TransferGate) : ForwardingDataSource(upstream) {
    override fun open(dataSpec: DataSpec): Long {
        gate.awaitOpen()
        return upstream.open(dataSpec)
    }
}

@UnstableApi
class BlockingHold(private val gate: TransferGate) : TransferHold {
    override fun upstream(http: DataSource.Factory): DataSource.Factory =
        DataSource.Factory { GatedDataSource(http.createDataSource(), gate) }
}
