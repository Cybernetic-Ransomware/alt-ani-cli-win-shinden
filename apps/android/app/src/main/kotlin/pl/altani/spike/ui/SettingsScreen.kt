package pl.altani.spike.ui

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import pl.altani.spike.ui.SettingsFlow.SaveResult

private val HEIGHT_PRESETS = listOf(480, 720, 1080, 1440, 2160)

/** Controls only edit the draft held by [SettingsViewModel]; the bridge is called from the Save, Retry and Keep buttons alone. */
@Composable
fun SettingsScreen(model: SettingsViewModel) {
    val flow = model.flow
    val state by flow.state.collectAsStateWithLifecycle()
    val form = state.form
    if (form == null) {
        Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            val error = state.loadError
            if (error != null && !state.loading) {
                Text("Settings could not be read. ${error.message}", color = MaterialTheme.colorScheme.error)
                OutlinedButton(onClick = flow::refresh) { Text("Retry") }
            } else {
                CircularProgressIndicator()
            }
        }
        return
    }
    val enabled = !state.saving
    val problems = state.problems
    Column(Modifier.fillMaxSize()) {
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            state.loadError?.takeIf { state.unverified == null }?.let {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Could not refresh. ${it.message}", Modifier.weight(1f), color = MaterialTheme.colorScheme.error)
                    OutlinedButton(onClick = flow::refresh) { Text("Retry") }
                }
            }
            if (state.conflicts.isNotEmpty()) ConflictCard(state.conflicts, enabled, flow::resolve)

            Section("Playback preferences", "Used for the next episode you start. The video that is playing keeps its settings.")
            Label("Audio language")
            Choice("Any language", form.audioMode == LanguageMode.ANY, enabled) { flow.edit { it.copy(audioMode = LanguageMode.ANY) } }
            Choice("Specific language", form.audioMode == LanguageMode.SPECIFIC, enabled) { flow.edit { it.copy(audioMode = LanguageMode.SPECIFIC) } }
            if (form.audioMode == LanguageMode.SPECIFIC) {
                LanguageField(form.audioText, enabled, PreferenceField.AUDIO in problems, "Enter a language; \"none\" is for subtitles only.") { text ->
                    flow.edit { it.copy(audioText = text) }
                }
            }
            Hint("As the source labels it. No fallback: if no source has this audio, nothing is picked.")

            Label("Subtitles")
            Choice("Any (no preference)", form.subsMode == SubsMode.ANY, enabled) { flow.edit { it.copy(subsMode = SubsMode.ANY) } }
            Choice("None (no subtitles required)", form.subsMode == SubsMode.NONE, enabled) { flow.edit { it.copy(subsMode = SubsMode.NONE) } }
            Choice("Specific language", form.subsMode == SubsMode.SPECIFIC, enabled) { flow.edit { it.copy(subsMode = SubsMode.SPECIFIC) } }
            if (form.subsMode == SubsMode.SPECIFIC) {
                LanguageField(form.subsText, enabled, PreferenceField.SUBS in problems, "Enter a language.") { text -> flow.edit { it.copy(subsText = text) } }
            }
            if (form.subsMode == SubsMode.NONE) Hint("Only sources that declare no subtitles match. A source that does not say is not one.")

            Label("Preferred quality")
            Choice("Best available", form.qualityMode == QualityMode.BEST, enabled) { flow.edit { it.copy(qualityMode = QualityMode.BEST) } }
            Choice("At most…", form.qualityMode == QualityMode.MAX, enabled) {
                flow.edit { it.copy(qualityMode = QualityMode.MAX, heightText = it.heightText.ifBlank { "1080" }) }
            }
            if (form.qualityMode == QualityMode.MAX) {
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    HEIGHT_PRESETS.forEach { height ->
                        FilterChip(
                            selected = form.heightText.trim() == height.toString(),
                            onClick = { flow.edit { it.copy(heightText = height.toString()) } },
                            label = { Text("${height}p") },
                            enabled = enabled,
                        )
                    }
                }
                OutlinedTextField(
                    value = form.heightText,
                    onValueChange = { text -> flow.edit { it.copy(heightText = text.filter(Char::isDigit).take(5)) } },
                    label = { Text("Maximum height (pixels)") },
                    singleLine = true,
                    enabled = enabled,
                    isError = PreferenceField.QUALITY in problems,
                    supportingText = if (PreferenceField.QUALITY in problems) ({ Text("Enter a height from 1 to 99999.") }) else null,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            Hint("An upper limit: the best variant at or below it. Variants of unknown height never match a limit.")

            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            Section("Download preferences", "Downloads are not available in this version yet; these settings will apply to them.")
            Label("Download network")
            Choice("Wi-Fi only", form.downloadNetwork == DownloadNetwork.WIFI_ONLY, enabled) { flow.edit { it.copy(downloadNetwork = DownloadNetwork.WIFI_ONLY) } }
            Choice("Wi-Fi and mobile data", form.downloadNetwork == DownloadNetwork.ANY, enabled) { flow.edit { it.copy(downloadNetwork = DownloadNetwork.ANY) } }
            Hint("Downloads only, queued ones included. Watching still asks before using mobile data for each episode.")

            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Try alternative sources automatically", Modifier.weight(1f))
                Switch(checked = form.autoAlternatives, onCheckedChange = { on -> flow.edit { it.copy(autoAlternatives = on) } }, enabled = enabled)
            }
            Hint("When a download cannot resume from its source, try another source matching your audio, subtitle and quality settings. It never changes the language or the quality.")

            Label("Concurrent downloads")
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                val count = form.maxActiveDownloads
                OutlinedButton(
                    onClick = { flow.edit { it.copy(maxActiveDownloads = (it.maxActiveDownloads - 1).coerceAtMost(SettingsForm.MAX_ACTIVE.last)) } },
                    enabled = enabled && count > SettingsForm.MAX_ACTIVE.first,
                ) { Text("−") }
                Text(count.toString(), style = MaterialTheme.typography.titleMedium)
                OutlinedButton(
                    onClick = { flow.edit { it.copy(maxActiveDownloads = it.maxActiveDownloads + 1) } },
                    enabled = enabled && count < SettingsForm.MAX_ACTIVE.last,
                ) { Text("+") }
            }
        }
        ActionBar(state, flow)
    }
}

@Composable
private fun ActionBar(state: SettingsFlow.State, flow: SettingsFlow) {
    val result = state.result
    val dirty = state.dirty.isNotEmpty()
    val unverified = state.unverified != null && !state.saving
    if (!dirty && !state.saving && result == null && !unverified) return
    Surface(tonalElevation = 3.dp) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            val text = when {
                state.saving -> "Saving…"
                unverified -> "Could not confirm the save. ${state.loadError?.message.orEmpty()}"
                result is SaveResult.Failed -> "Not saved. ${result.message}"
                result == SaveResult.Rebased -> "Settings were changed elsewhere. Your changes were kept; review and save again."
                result == SaveResult.Saved && !dirty -> "Saved."
                else -> "Unsaved changes"
            }
            Text(
                text,
                Modifier.weight(1f),
                color = if (result is SaveResult.Failed || unverified) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
            )
            if (state.saving) {
                CircularProgressIndicator(Modifier.size(24.dp))
            } else if (unverified) {
                Button(onClick = flow::retryVerification) { Text("Retry") }
            } else if (dirty) {
                OutlinedButton(onClick = flow::cancel) { Text("Cancel") }
                Button(onClick = flow::save, enabled = state.canSave) { Text("Save") }
            }
        }
    }
}

@Composable
private fun ConflictCard(conflicts: List<SettingsFlow.Conflict>, enabled: Boolean, resolve: (Boolean) -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Settings were changed elsewhere", fontWeight = FontWeight.Bold)
            conflicts.forEach { Text("${label(it.field)}: yours ${it.mine}, current ${it.current}") }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { resolve(true) }, enabled = enabled) { Text("Keep mine and save") }
                OutlinedButton(onClick = { resolve(false) }, enabled = enabled) { Text("Use current") }
            }
        }
    }
}

@Composable
private fun Section(title: String, note: String) {
    Text(title, style = MaterialTheme.typography.titleLarge)
    Hint(note)
}

@Composable
private fun Label(text: String) {
    Text(text, style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(top = 8.dp))
}

@Composable
private fun Hint(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun Choice(text: String, selected: Boolean, enabled: Boolean, onSelect: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().selectable(selected = selected, enabled = enabled, role = Role.RadioButton, onClick = onSelect),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = selected, onClick = null, enabled = enabled)
        Text(text, Modifier.padding(start = 8.dp))
    }
}

@Composable
private fun LanguageField(value: String, enabled: Boolean, error: Boolean, errorText: String, onChange: (String) -> Unit) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text("Language") },
        placeholder = { Text("e.g. jp") },
        singleLine = true,
        enabled = enabled,
        isError = error,
        supportingText = if (error) ({ Text(errorText) }) else null,
        modifier = Modifier.fillMaxWidth(),
    )
}

private fun label(field: PreferenceField): String =
    when (field) {
        PreferenceField.AUDIO -> "Audio language"
        PreferenceField.SUBS -> "Subtitles"
        PreferenceField.QUALITY -> "Preferred quality"
        PreferenceField.DOWNLOAD_NETWORK -> "Download network"
        PreferenceField.AUTO_ALTERNATIVES -> "Automatic alternatives"
        PreferenceField.MAX_ACTIVE_DOWNLOADS -> "Concurrent downloads"
    }
