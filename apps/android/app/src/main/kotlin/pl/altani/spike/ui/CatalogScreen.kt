package pl.altani.spike.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import androidx.media3.ui.PlayerView
import pl.altani.spike.playback.PlaybackController
import pl.altani.spike.ui.CatalogFlow.Load

@UnstableApi
@Composable
fun CatalogScreen(catalog: CatalogViewModel, playback: PlaybackViewModel) {
    val flow = catalog.flow
    val state by flow.state.collectAsStateWithLifecycle()
    val mode by playback.mode.collectAsStateWithLifecycle()
    val player by playback.catalogController.collectAsStateWithLifecycle()
    var query by rememberSaveable { mutableStateOf(state.query) }

    BackHandler(enabled = state.series != null) { flow.back() }

    Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                label = { Text("Search Shinden") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { flow.search(query) }),
                modifier = Modifier.weight(1f),
            )
            if (state.busy) {
                OutlinedButton(onClick = flow::cancel) { Text("Cancel") }
            } else {
                Button(onClick = { flow.search(query) }, enabled = query.isNotBlank()) { Text("Search") }
            }
        }
        player?.takeIf { mode == PlaybackViewModel.Mode.CATALOG }?.let { PlayerPanel(it, playback) }
        val series = state.series
        if (series == null) {
            SearchResults(state.search, flow)
        } else {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = flow::back) { Text("Back") }
                Text(series.title, style = MaterialTheme.typography.titleMedium)
            }
            Episodes(state.episodes, state.episode, flow) { picked ->
                flow.pickEpisode(picked)?.let { (ref, episode) -> playback.playCatalog(ref, episode) }
            }
        }
    }
}

@UnstableApi
@Composable
private fun PlayerPanel(controller: PlaybackController, playback: PlaybackViewModel) {
    val state by controller.state.collectAsStateWithLifecycle()
    val seen by playback.evidence.snapshot.collectAsStateWithLifecycle()
    AndroidView(
        factory = { PlayerView(it).apply { player = controller.player } },
        onRelease = { it.player = null },
        modifier = Modifier.fillMaxWidth().height(200.dp),
    )
    val stream = when {
        state.mode == null -> "not resolved"
        state.mode == "online" -> "online ${state.variant ?: ""}"
        else -> "local ${state.mode}"
    }
    val media = when {
        seen.confirmed -> "confirmed (${seen.progressedMs / 1000}s played${if (seen.hasVideo) ", first frame" else ", audio only"})"
        else -> "not confirmed (progress ${seen.progressedMs / 1000}s, first frame ${seen.firstFrame})"
    }
    Text("phase: ${state.phase} · stream: $stream · media3: $media", style = MaterialTheme.typography.bodySmall)
    if (state.consentRequired) {
        Button(onClick = { controller.allowCellular() }) { Text("Allow mobile data for this episode") }
    }
    state.error?.let { code ->
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(CatalogMessages.describe(code), color = MaterialTheme.colorScheme.error, modifier = Modifier.weight(1f))
            OutlinedButton(onClick = playback::retryCatalog) { Text("Retry") }
        }
    }
}

@Composable
private fun SearchResults(load: Load<SearchPage>, flow: CatalogFlow) {
    when (load) {
        Load.Idle -> Unit
        Load.Loading -> Busy("Searching…")
        is Load.Failed -> Failure(load, flow)
        is Load.Ready -> {
            val page = load.value
            if (page.hits.isEmpty()) return Text("No results for \"${page.query}\".")
            page.incompleteReason?.let { Note("Showing the first ${page.hits.size} of ${page.total ?: "?"} results ($it).") }
            LazyColumn {
                items(page.hits) { hit ->
                    Row(Modifier.fillMaxWidth().clickable { flow.selectSeries(hit) }.padding(vertical = 12.dp)) {
                        Text(hit.title, modifier = Modifier.weight(1f))
                        Text(hit.type, style = MaterialTheme.typography.bodySmall)
                    }
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun Episodes(load: Load<EpisodePage>, chosen: Episode?, flow: CatalogFlow, onPick: (Episode) -> Unit) {
    when (load) {
        Load.Idle -> Unit
        Load.Loading -> Busy("Loading episodes…")
        is Load.Failed -> Failure(load, flow)
        is Load.Ready -> {
            val page = load.value
            if (page.episodes.isEmpty()) return Text("No episodes available online.")
            page.incompleteReason?.let { Note("Episode list incomplete ($it): the oldest episodes may be missing.") }
            LazyColumn {
                items(page.episodes.sortedBy { it.number }) { episode ->
                    val weight = if (episode == chosen) FontWeight.Bold else FontWeight.Normal
                    Text(
                        "${episode.label}. ${episode.title}",
                        fontWeight = weight,
                        modifier = Modifier.fillMaxWidth().clickable { onPick(episode) }.padding(vertical = 12.dp),
                    )
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun Busy(label: String) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        CircularProgressIndicator(Modifier.height(20.dp))
        Text(label)
    }
}

@Composable
private fun Failure(failed: Load.Failed, flow: CatalogFlow) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(failed.message, color = MaterialTheme.colorScheme.error, modifier = Modifier.weight(1f))
        OutlinedButton(onClick = flow::retry) { Text("Retry") }
    }
}

@Composable
private fun Note(text: String) = Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary)
