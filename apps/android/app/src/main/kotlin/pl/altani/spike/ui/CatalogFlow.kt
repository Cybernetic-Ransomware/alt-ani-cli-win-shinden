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

/** search → series → episodes on one coroutine at a time; a newer choice cancels the older call and drops its reply. */
class CatalogFlow(
    private val scope: CoroutineScope,
    private val call: suspend (method: String, args: JSONObject) -> BridgeResult,
    private val log: (String) -> Unit = {},
    private val clock: () -> Long = System::currentTimeMillis,
) {
    sealed interface Load<out T> {
        data object Idle : Load<Nothing>

        data object Loading : Load<Nothing>

        data class Ready<T>(val value: T) : Load<T>

        data class Failed(val code: String, val message: String) : Load<Nothing>
    }

    data class State(
        val query: String = "",
        val search: Load<SearchPage> = Load.Idle,
        val series: SeriesHit? = null,
        val episodes: Load<EpisodePage> = Load.Idle,
        val episode: Episode? = null,
    ) {
        val busy: Boolean get() = search == Load.Loading || episodes == Load.Loading
    }

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()

    private var job: Job? = null

    // bumped by every new choice, so a reply that came back despite its cancellation is still dropped
    private var generation = 0

    fun search(query: String) {
        val text = query.trim()
        if (text.isEmpty()) return
        val started = supersede()
        _state.value = State(query = text, search = Load.Loading)
        job = scope.launch {
            val reply = timed("search") { call("search", JSONObject().put("query", text)) }
            if (started != generation) return@launch
            val load = reply.toLoad { SearchPage.parse(text, it) }
            if (load is Load.Ready) log("search hits=${load.value.hits.size} complete=${load.value.incompleteReason == null}")
            _state.update { it.copy(search = load) }
        }
    }

    fun selectSeries(hit: SeriesHit) {
        val started = supersede()
        _state.update { it.copy(series = hit, episodes = Load.Loading, episode = null) }
        job = scope.launch {
            val reply = timed("episodes") { call("episodes", JSONObject().put("series", hit.ref().toJson())) }
            if (started != generation) return@launch
            val load = reply.toLoad(EpisodePage::parse)
            if (load is Load.Ready) log("episodes count=${load.value.episodes.size} complete=${load.value.incompleteReason == null}")
            _state.update { it.copy(episodes = load) }
        }
    }

    /** The series the bridge returned with the listing, not the search hit, goes to playback. */
    fun pickEpisode(episode: Episode): Pair<SeriesRef, Episode>? {
        val page = (_state.value.episodes as? Load.Ready)?.value ?: return null
        if (episode !in page.episodes) return null
        _state.update { it.copy(episode = episode) }
        return page.series to episode
    }

    fun back() {
        supersede()
        _state.update { it.copy(series = null, episodes = Load.Idle, episode = null) }
    }

    fun cancel() {
        val current = _state.value
        when {
            current.episodes == Load.Loading -> back()
            current.search == Load.Loading -> {
                supersede()
                _state.update { it.copy(search = Load.Idle) }
            }
        }
    }

    fun retry() {
        val current = _state.value
        val series = current.series
        when {
            series != null && current.episodes is Load.Failed -> selectSeries(series)
            current.search is Load.Failed -> search(current.query)
        }
    }

    private fun supersede(): Int {
        job?.cancel()
        job = null
        return ++generation
    }

    private suspend fun timed(method: String, body: suspend () -> BridgeResult): BridgeResult {
        val start = clock()
        val reply = body()
        log("$method ${outcome(reply)} ms=${clock() - start}")
        return reply
    }

    private fun <T> BridgeResult.toLoad(parse: (JSONObject) -> T): Load<T> =
        when (this) {
            is BridgeResult.Ok ->
                try {
                    Load.Ready(parse(data))
                } catch (e: JSONException) {
                    failed("malformed_reply", "internal")
                }
            is BridgeResult.Failure -> failed(code, kind)
            BridgeResult.NotStarted -> failed("cancelled", "cancelled")
        }

    private fun failed(code: String, kind: String): Load.Failed = Load.Failed(code, CatalogMessages.describe(code, kind))

    private companion object {
        fun outcome(reply: BridgeResult): String =
            when (reply) {
                is BridgeResult.Ok -> "ok"
                is BridgeResult.Failure -> "${reply.kind}/${reply.code}"
                BridgeResult.NotStarted -> "not_started"
            }
    }
}
