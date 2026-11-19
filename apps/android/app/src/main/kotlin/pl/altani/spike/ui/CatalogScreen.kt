package pl.altani.spike.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
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
import androidx.compose.material3.FilterChip
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
import androidx.compose.ui.graphics.Color
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
fun CatalogScreen(catalog: CatalogViewModel, playback: PlaybackViewModel, onFullscreen: (Boolean) -> Unit) {
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
        player?.takeIf { mode == PlaybackViewModel.Mode.CATALOG }?.let { PlayerPanel(it, playback, flow, onFullscreen) }
        val series = state.series
        val episode = state.episode
        when {
            series == null -> SearchResults(state, flow)
            episode == null -> {
                Header(series.title, flow)
                Episodes(state.episodes, flow)
            }
            else -> {
                Header("${series.title} · ${episode.label}. ${episode.title}", flow)
                Sources(state.sources, state.source, flow) { picked -> flow.pickSource(picked)?.let(playback::playCatalog) }
            }
        }
    }
}

@UnstableApi
@Composable
private fun PlayerPanel(controller: PlaybackController, playback: PlaybackViewModel, flow: CatalogFlow, onFullscreen: (Boolean) -> Unit) {
    val state by controller.state.collectAsStateWithLifecycle()
    val seen by playback.evidence.snapshot.collectAsStateWithLifecycle()
    CatalogPlayerView(controller, fullscreen = false, onFullscreen, Modifier.fillMaxWidth().height(200.dp))
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
            if (CatalogMessages.sessionGone(code)) {
                OutlinedButton(onClick = flow::reloadSources) { Text("Reload sources") }
            } else {
                OutlinedButton(onClick = playback::retryCatalog) { Text("Retry") }
            }
        }
    }
}

@UnstableApi
@Composable
fun FullscreenPlayer(controller: PlaybackController, onExit: () -> Unit) {
    BackHandler(onBack = onExit)
    CatalogPlayerView(controller, fullscreen = true, { if (!it) onExit() }, Modifier.fillMaxSize().background(Color.Black))
}

@UnstableApi
@Composable
private fun CatalogPlayerView(controller: PlaybackController, fullscreen: Boolean, onFullscreen: (Boolean) -> Unit, modifier: Modifier) {
    AndroidView(
        factory = { PlayerView(it).apply { player = controller.player } },
        update = { view ->
            view.setFullscreenButtonState(fullscreen)
            view.setFullscreenButtonClickListener { onFullscreen(it) }
        },
        onRelease = { it.player = null },
        modifier = modifier,
    )
}

@Composable
private fun SearchResults(state: CatalogFlow.State, flow: CatalogFlow) {
    when (val load = state.search) {
        Load.Idle -> Unit
        Load.Loading -> Busy("Searching…")
        is Load.Failed -> Failure(load, flow)
        is Load.Ready -> {
            val page = load.value
            if (page.hits.isEmpty()) return Text("No results for \"${page.query}\".")
            page.incompleteReason?.let { Note("Showing the first ${page.hits.size} of ${page.total ?: "?"} results ($it).") }
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(state.order == HitOrder.RELEVANCE, { flow.orderBy(HitOrder.RELEVANCE) }, { Text("Best match") })
                FilterChip(state.order == HitOrder.NEWEST, { flow.orderBy(HitOrder.NEWEST) }, { Text("Newest first") })
            }
            val dates = (state.airDates as? Load.Ready)?.value
            when (val airDates = state.airDates) {
                Load.Loading -> Busy("Reading air dates…")
                is Load.Failed -> Text("Air dates unavailable: ${airDates.message}", color = MaterialTheme.colorScheme.error)
                is Load.Ready ->
                    airDates.value.values.count { it == null }.takeIf { it > 0 && state.order == HitOrder.NEWEST }?.let {
                        Note("No air date for $it of ${page.hits.size} titles; they are listed last.")
                    }
                Load.Idle -> Unit
            }
            LazyColumn {
                items(state.hits) { hit ->
                    Row(Modifier.fillMaxWidth().clickable { flow.selectSeries(hit) }.padding(vertical = 12.dp)) {
                        Text(hit.title, modifier = Modifier.weight(1f))
                        val year = dates?.get(hit.id)?.year?.takeIf { it > 0 }
                        Text(listOfNotNull(hit.type.ifEmpty { null }, year).joinToString(" · "), style = MaterialTheme.typography.bodySmall)
                    }
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun Episodes(load: Load<EpisodePage>, flow: CatalogFlow) {
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
                    Text(
                        "${episode.label}. ${episode.title}",
                        modifier = Modifier.fillMaxWidth().clickable { flow.openEpisode(episode) }.padding(vertical = 12.dp),
                    )
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun Sources(load: Load<SourcePage>, chosen: String?, flow: CatalogFlow, onPick: (Source?) -> Unit) {
    when (load) {
        Load.Idle -> Unit
        Load.Loading -> Busy("Loading sources…")
        is Load.Failed -> Failure(load, flow)
        is Load.Ready -> {
            val page = load.value
            if (page.sources.isEmpty()) return Text("No players listed for this episode.")
            LazyColumn {
                item { SourceRow("Auto (by preferences)", chosen == CatalogFlow.AUTO) { onPick(null) } }
                items(page.sources) { source -> SourceRow(source.label, chosen == source.onlineId) { onPick(source) } }
            }
        }
    }
}

@Composable
private fun SourceRow(label: String, chosen: Boolean, onClick: () -> Unit) {
    Text(
        label,
        fontWeight = if (chosen) FontWeight.Bold else FontWeight.Normal,
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 12.dp),
    )
    HorizontalDivider()
}

@Composable
private fun Header(title: String, flow: CatalogFlow) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = flow::back) { Text("Back") }
        Text(title, style = MaterialTheme.typography.titleMedium)
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
