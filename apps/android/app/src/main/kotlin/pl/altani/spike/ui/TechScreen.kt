package pl.altani.spike.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import androidx.media3.ui.PlayerView
import org.json.JSONObject

@UnstableApi
@Composable
fun TechScreen(model: TechViewModel, playback: PlaybackViewModel) {
    val log by model.log.collectAsStateWithLifecycle()
    val pump by model.pump.collectAsStateWithLifecycle()
    val playbackLog by playback.log.collectAsStateWithLifecycle()
    Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = model::bridgeInfo) { Text("bridge_info") }
            Button(onClick = model::engineStatus) { Text("engine_status") }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = model::writeWatch) { Text("T4 write") }
            Button(onClick = model::readWatch) { Text("T4 read") }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = model::toggleEvents) { Text(if (pump.running) "Stop events" else "Start events") }
            Button(onClick = model::feasibility) { Text("Checks") }
        }
        Text(
            "events: running=${pump.running} heads=${pump.heads} next_seq=${pump.nextSeq} received=${pump.received}" +
                (pump.lastError?.let { " error=$it" } ?: ""),
            style = MaterialTheme.typography.bodySmall,
        )
        playback.controller?.let { controller ->
            val state by controller.state.collectAsStateWithLifecycle()
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("progressive" to "MP4", "hls" to "HLS", "dash" to "DASH").forEach { (kind, label) ->
                    Button(onClick = { playback.playControlled(JSONObject().put("kind", kind)) }) { Text(label) }
                }
                Button(onClick = { playback.playControlled(JSONObject().put("kind", "progressive"), local = true) }) { Text("Local") }
            }
            Button(onClick = { playback.playControlled(JSONObject().put("kind", "hls"), episodeIndex = 1) }) { Text("HLS, next episode") }
            Text(
                "playback: ${state.phase} ${state.mode ?: ""} ${state.variant ?: ""} refreshes=${state.refreshes} " +
                    "variant_changes=${state.variantChanges}" + (state.error?.let { " error=$it" } ?: ""),
                style = MaterialTheme.typography.bodySmall,
            )
            if (state.consentRequired) {
                Button(onClick = { controller.allowCellular() }) { Text("Allow mobile data for this episode") }
            }
            AndroidView(
                factory = { PlayerView(it).apply { player = controller.player } },
                onRelease = { it.player = null },
                modifier = Modifier.fillMaxWidth().height(180.dp),
            )
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            items((playbackLog + log).asReversed()) { Text(it, style = MaterialTheme.typography.bodySmall) }
        }
    }
}
