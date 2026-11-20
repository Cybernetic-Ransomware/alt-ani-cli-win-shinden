package pl.altani.spike.ui

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONException
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.LateResult
import pl.altani.spike.ui.CatalogFlow.Load

/** Only newer snapshots apply, merged three-way into the draft; a save sends just the fields the user edited. */
class SettingsFlow(
    private val scope: CoroutineScope,
    private val call: suspend (method: String, args: JSONObject) -> BridgeResult,
    private val log: (String) -> Unit = {},
) {
    sealed interface SaveResult {
        data object Saved : SaveResult

        /** `state.conflict` without a clash: the draft now sits on the newer preferences and waits for another Save. */
        data object Rebased : SaveResult

        data class Failed(val code: String, val message: String) : SaveResult
    }

    data class Conflict(val field: PreferenceField, val mine: String, val current: String)

    data class State(
        // the newest preferences applied; the draft is always relative to them
        val base: Preferences? = null,
        val form: SettingsForm? = null,
        val loading: Boolean = false,
        val loadError: Load.Failed? = null,
        val saving: Boolean = false,
        val result: SaveResult? = null,
        val conflicts: List<Conflict> = emptyList(),
    ) {
        val dirty: Set<PreferenceField>
            get() = if (base == null || form == null) emptySet() else form.dirty(base.values)

        val problems: Set<PreferenceField>
            get() = form?.problems(dirty) ?: emptySet()

        val canSave: Boolean
            get() = base != null && !saving && conflicts.isEmpty() && dirty.isNotEmpty() && problems.isEmpty()
    }

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()

    private var readJob: Job? = null

    // a refresh asked for while a read is in flight: that read may predate the change, so one more follows it
    private var readStale = false

    fun refresh() {
        if (readJob?.isActive == true) {
            readStale = true
            return
        }
        _state.update { it.copy(loading = true) }
        readJob = scope.launch {
            do {
                readStale = false
                read()
            } while (readStale)
            _state.update { it.copy(loading = false) }
        }
    }

    /** `preferences.changed`; our own save's signal carries a revision already held and is ignored. */
    fun onEvent(event: JSONObject) {
        if (event.optString("type") != CHANGED_EVENT) return
        val revision = event.optJSONObject("data")?.opt("revision") as? Number ?: return refresh()
        val held = _state.value.base?.revision
        if (held == null || revision.toLong() > held) refresh()
    }

    /** A save no caller received may still have been written: only a read can tell. */
    fun onLateResult(late: LateResult) {
        if (late.method == UPDATE && late.needsRefresh) refresh()
    }

    fun edit(transform: (SettingsForm) -> SettingsForm) {
        _state.update { s ->
            val form = s.form ?: return@update s
            val base = s.base ?: return@update s
            if (s.saving) return@update s
            val next = transform(form)
            val saved = SettingsForm.from(base.values)
            // a conflict the user settled by hand, picking the current value, is no longer one
            s.copy(form = next, result = null, conflicts = s.conflicts.filter { next.intent(it.field) != saved.intent(it.field) })
        }
    }

    fun cancel() {
        _state.update { s ->
            val base = s.base ?: return@update s
            if (s.saving) s else s.copy(form = SettingsForm.from(base.values), conflicts = emptyList(), result = null)
        }
    }

    /** [keepMine] is the user's explicit choice to write their values over the current ones; it saves at once. */
    fun resolve(keepMine: Boolean) {
        _state.update { s ->
            val base = s.base ?: return@update s
            val form = s.form ?: return@update s
            if (s.saving) return@update s
            val current = SettingsForm.from(base.values)
            val resolved = if (keepMine) form else s.conflicts.fold(form) { f, c -> f.take(c.field, current) }
            s.copy(form = resolved, conflicts = emptyList(), result = null)
        }
        if (keepMine) save()
    }

    fun save() {
        val s = _state.value
        if (!s.canSave) return
        val base = s.base ?: return
        val changes = s.form?.changes(base.values) ?: return
        _state.update { it.copy(saving = true, result = null) }
        scope.launch {
            val args = JSONObject().put("changes", changes).put("expected_revision", base.revision)
            val reply = call(UPDATE, args)
            log("$UPDATE ${outcome(reply)} fields=${changes.length()} expected=${base.revision}")
            when {
                reply is BridgeResult.Ok -> {
                    val saved = try {
                        Preferences.parse(reply.data)
                    } catch (e: JSONException) {
                        null
                    }
                    if (saved != null) {
                        apply(saved)
                        settle(SaveResult.Saved)
                    } else {
                        verify(failed("malformed_reply", "internal"))
                    }
                }
                reply is BridgeResult.Failure && reply.code == CONFLICT -> verify(SaveResult.Rebased)
                // refused while reading the request, before anything was written
                reply is BridgeResult.Failure && reply.kind == "invalid_input" -> settle(failed(reply.code, reply.kind))
                reply is BridgeResult.Failure -> verify(failed(reply.code, reply.kind))
                else -> verify(failed("cancelled", "cancelled"))
            }
        }
    }

    /** The outcome is unknown or the base moved: read the preferences before the user may save again. */
    private suspend fun verify(fallback: SaveResult) {
        read()
        settle(fallback)
    }

    private fun settle(fallback: SaveResult) {
        _state.update { s ->
            val base = s.base
            when {
                s.conflicts.isNotEmpty() -> s.copy(saving = false, result = null)
                // every edited field now holds what the user asked for: written by this save, an earlier one or elsewhere
                base != null && s.dirty.isEmpty() -> s.copy(saving = false, result = SaveResult.Saved, form = SettingsForm.from(base.values))
                else -> s.copy(saving = false, result = fallback)
            }
        }
    }

    private suspend fun read() {
        val reply = call(GET, JSONObject())
        val snapshot = when (reply) {
            is BridgeResult.Ok -> try {
                Preferences.parse(reply.data)
            } catch (e: JSONException) {
                null
            }
            else -> null
        }
        log("$GET ${if (reply is BridgeResult.Ok && snapshot == null) "malformed" else outcome(reply)} revision=${snapshot?.revision}")
        if (snapshot != null) {
            apply(snapshot)
            _state.update { it.copy(loadError = null) }
            return
        }
        val error = when (reply) {
            is BridgeResult.Ok -> failed("malformed_reply", "internal")
            is BridgeResult.Failure -> failed(reply.code, reply.kind)
            BridgeResult.NotStarted -> failed("cancelled", "cancelled")
        }
        _state.update { it.copy(loadError = Load.Failed(error.code, error.message)) }
    }

    private fun apply(next: Preferences) {
        _state.update { s ->
            val base = s.base
            val form = s.form
            when {
                base == null || form == null -> s.copy(base = next, form = SettingsForm.from(next.values))
                next.revision <= base.revision -> {
                    if (next.revision < base.revision) log("preferences revision=${next.revision} older than ${base.revision}, dropped")
                    s
                }
                s.dirty.isEmpty() && s.conflicts.isEmpty() -> s.copy(base = next, form = SettingsForm.from(next.values))
                else -> {
                    val (merged, conflicts) = merge(base.values, form, next.values, s.conflicts)
                    if (conflicts.isNotEmpty()) log("preferences conflict fields=${conflicts.joinToString(",") { it.field.wire }}")
                    s.copy(base = next, form = merged, conflicts = conflicts)
                }
            }
        }
    }

    companion object {
        const val GET = "preferences_get"
        const val UPDATE = "preferences_update"
        const val CHANGED_EVENT = "preferences.changed"
        private const val CONFLICT = "state.conflict"

        /** Equal values never clash, one-sided changes win; a conflict stays until the values agree or the user picks a side. */
        fun merge(old: PreferenceValues, draft: SettingsForm, next: PreferenceValues, shown: List<Conflict>): Pair<SettingsForm, List<Conflict>> {
            val was = SettingsForm.from(old)
            val now = SettingsForm.from(next)
            var merged = draft
            val conflicts = mutableListOf<Conflict>()
            for (field in PreferenceField.entries) {
                val mine = draft.intent(field)
                val pending = shown.any { it.field == field }
                when {
                    mine == now.intent(field) -> {}
                    mine == was.intent(field) && !pending -> merged = merged.take(field, now)
                    now.intent(field) == was.intent(field) && !pending -> {}
                    else -> conflicts += Conflict(field, draft.describe(field), now.describe(field))
                }
            }
            return merged to conflicts
        }

        private fun failed(code: String, kind: String): SaveResult.Failed = SaveResult.Failed(code, describe(code, kind))

        /** Built from the code alone, like [CatalogMessages]; the engine's own message is never shown. */
        fun describe(code: String, kind: String?): String =
            when {
                code == "malformed_reply" -> "The engine answered in an unexpected form."
                code == "cancelled" || kind == "cancelled" -> "Cancelled."
                kind == "closed" -> "The engine is closed. Restart the app. ($code)"
                kind == "invalid_input" -> "The engine refused these values. ($code)"
                else -> "The engine could not be reached. ($code)"
            }

        private fun outcome(reply: BridgeResult): String =
            when (reply) {
                is BridgeResult.Ok -> "ok"
                is BridgeResult.Failure -> "${reply.kind}/${reply.code}"
                BridgeResult.NotStarted -> "not_started"
            }
    }
}
