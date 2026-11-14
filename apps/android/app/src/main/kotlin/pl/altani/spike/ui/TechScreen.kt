package pl.altani.spike.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
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
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@Composable
fun TechScreen(model: TechViewModel) {
    val log by model.log.collectAsStateWithLifecycle()
    val pump by model.pump.collectAsStateWithLifecycle()
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
        LazyColumn(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            items(log.asReversed()) { Text(it, style = MaterialTheme.typography.bodySmall) }
        }
    }
}
