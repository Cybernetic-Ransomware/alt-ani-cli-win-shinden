package pl.altani.spike.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import androidx.media3.ui.PlayerView
import org.json.JSONObject
import pl.altani.spike.offline.background.RunTarget

@UnstableApi
@Composable
fun TechScreen(model: TechViewModel, playback: PlaybackViewModel) {
    val log by model.log.collectAsStateWithLifecycle()
    val pump by model.pump.collectAsStateWithLifecycle()
    val playbackLog by playback.log.collectAsStateWithLifecycle()
    val run by model.runState.collectAsStateWithLifecycle()
    val context = LocalContext.current
    var afterPermission by remember { mutableStateOf<(() -> Unit)?>(null) }
    // asked when a background session is requested; refused or not, the download goes on without a visible notification
    val notificationPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
        afterPermission?.invoke()
        afterPermission = null
    }
    val withNotifications: (() -> Unit) -> Unit = { action ->
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            afterPermission = action
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            action()
        }
    }
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
            Button(onClick = model::startEvents, enabled = !pump.running) { Text("Start events") }
            Button(onClick = model::feasibility) { Text("Checks") }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { withNotifications(model::backgroundDownload) }) { Text("Background download") }
            Button(onClick = { withNotifications { model.resumeBackground(RunTarget.CONTROLLED) } }) { Text("Resume") }
            Button(onClick = { withNotifications { model.resumeBackground(RunTarget.APP) } }) { Text("Resume app") }
        }
        Text(
            "offline run: gen=${run.generation} ${run.stage} ${run.target ?: ""} ${run.summary?.phase ?: ""}" +
                (run.lastEnd?.let { " last=$it" } ?: ""),
            style = MaterialTheme.typography.bodySmall,
        )
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
