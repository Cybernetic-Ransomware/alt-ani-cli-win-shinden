package pl.altani.spike.ui

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult

/** search → series → episodes → sources on one coroutine at a time; a newer choice cancels the older call and drops its reply. */
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
        val sources: Load<SourcePage> = Load.Idle,
        val source: String? = null,
        val order: HitOrder = HitOrder.RELEVANCE,
        val airDates: Load<Map<String, AirDate?>> = Load.Idle,
    ) {
        val busy: Boolean
            get() = search == Load.Loading || episodes == Load.Loading || sources == Load.Loading || airDates == Load.Loading

        /** The search hits in [order]; until the air dates arrive [HitOrder.NEWEST] shows the relevance order. */
        val hits: List<SeriesHit>
            get() {
                val page = (search as? Load.Ready)?.value ?: return emptyList()
                val ranked = HitOrder.relevance(page.query, page.hits)
                val dates = (airDates as? Load.Ready)?.value
                return if (order == HitOrder.NEWEST && dates != null) HitOrder.newest(ranked, dates) else ranked
            }
    }

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()

    private var job: Job? = null

    // bumped by every new choice, so a reply that came back despite its cancellation is still dropped
    private var generation = 0

    // episode sessions held in the engine: the one listed on screen and the one the player was given
    private var shown: String? = null
    private var playing: String? = null

    fun search(query: String) {
        val text = query.trim()
        if (text.isEmpty()) return
        val started = supersede()
        show(null)
        _state.value = State(query = text, search = Load.Loading)
        job = scope.launch {
            val reply = timed("search") { call("search", JSONObject().put("query", text)) }
            if (started != generation) return@launch
            val load = reply.toLoad { SearchPage.parse(text, it) }
            if (load is Load.Ready) log("search hits=${load.value.hits.size} complete=${load.value.incompleteReason == null}")
            _state.update { it.copy(search = load) }
        }
    }

    /** [HitOrder.NEWEST] reads the air dates of all hits once per search; choosing it again after a failure retries. */
    fun orderBy(order: HitOrder) {
        val current = _state.value
        val page = (current.search as? Load.Ready)?.value ?: return
        if (current.series != null) return
        if (order == HitOrder.RELEVANCE) {
            if (current.airDates == Load.Loading) supersede()
            _state.update { it.copy(order = order) }
            return
        }
        _state.update { it.copy(order = order) }
        if (current.airDates is Load.Ready || current.airDates == Load.Loading) return
        val started = supersede()
        _state.update { it.copy(airDates = Load.Loading) }
        job = scope.launch {
            val urls = page.hits.map { it.url }
            val reply = timed("metadata_many") { call("metadata_many", JSONObject().put("urls", JSONArray(urls))) }
            if (started != generation) return@launch
            val load = reply.toLoad { data ->
                val found = data.getJSONArray("metadata")
                if (found.length() != urls.size) throw JSONException("metadata count")
                page.hits.indices.associate { page.hits[it].id to AirDate.parse(found.optJSONObject(it)) }
            }
            if (load is Load.Ready) log("metadata_many dated=${load.value.values.count { it != null }} of ${urls.size}")
            _state.update { if (load is Load.Failed) it.copy(airDates = load, order = HitOrder.RELEVANCE) else it.copy(airDates = load) }
        }
    }

    fun selectSeries(hit: SeriesHit) {
        val started = supersede()
        show(null)
        _state.update { it.copy(series = hit, episodes = Load.Loading, episode = null, sources = Load.Idle, source = null) }
        job = scope.launch {
            val reply = timed("episodes") { call("episodes", JSONObject().put("series", hit.ref().toJson())) }
            if (started != generation) return@launch
            val load = reply.toLoad(EpisodePage::parse)
            if (load is Load.Ready) log("episodes count=${load.value.episodes.size} complete=${load.value.incompleteReason == null}")
            _state.update { it.copy(episodes = load) }
        }
    }

    fun openEpisode(episode: Episode) {
        val page = (_state.value.episodes as? Load.Ready)?.value ?: return
        if (episode !in page.episodes) return
        val started = supersede()
        show(null)
        _state.update { it.copy(episode = episode, sources = Load.Loading, source = null) }
        job = scope.launch {
            val reply = timed("open_episode") { call("open_episode", JSONObject().put("episode", episode.toJson())) }
            if (started != generation) {
                // the engine opened it anyway; nothing on screen will ever use it
                if (reply is BridgeResult.Ok) reply.data.optString("session_id").ifEmpty { null }?.let(::close)
                return@launch
            }
            val load = reply.toLoad { SourcePage.parse(episode, it) }
            if (load is Load.Ready) {
                show(load.value.sessionId)
                log("open_episode sources=${load.value.sources.size}")
            }
            _state.update { it.copy(sources = load) }
        }
    }

    fun pickSource(source: Source?): PlaybackPick? {
        val current = _state.value
        val series = (current.episodes as? Load.Ready)?.value?.series ?: return null
        val page = (current.sources as? Load.Ready)?.value ?: return null
        if (source != null && source !in page.sources) return null
        play(page.sessionId)
        _state.update { it.copy(source = source?.onlineId ?: AUTO) }
        return PlaybackPick(series, page.episode, page.sessionId, source?.onlineId)
    }

    /** The bridge closed a session (network change, access change); a listed one is opened again once. */
    fun onSessionClosed(sessionId: String) {
        if (playing == sessionId) playing = null
        if (shown != sessionId) return
        shown = null
        log("episode session closed, reopening")
        _state.value.episode?.let(::openEpisode)
    }

    fun reloadSources() {
        _state.value.episode?.let(::openEpisode)
    }

    fun back() {
        supersede()
        show(null)
        if (_state.value.episode != null) {
            _state.update { it.copy(episode = null, sources = Load.Idle, source = null) }
        } else {
            _state.update { it.copy(series = null, episodes = Load.Idle) }
        }
    }

    fun cancel() {
        val current = _state.value
        when {
            current.airDates == Load.Loading -> orderBy(HitOrder.RELEVANCE)
            current.sources == Load.Loading || current.episodes == Load.Loading -> back()
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
            current.episode != null && current.sources is Load.Failed -> openEpisode(current.episode)
            series != null && current.episodes is Load.Failed -> selectSeries(series)
            current.search is Load.Failed -> search(current.query)
        }
    }

    private fun show(sessionId: String?) {
        val old = shown
        shown = sessionId
        if (old != null && old != sessionId && old != playing) close(old)
    }

    private fun play(sessionId: String) {
        val old = playing
        playing = sessionId
        if (old != null && old != sessionId && old != shown) close(old)
    }

    // not tied to the current job: a newer choice must not stop the cleanup of an older session
    private fun close(sessionId: String) {
        scope.launch { timed("close_session") { call("close_session", JSONObject().put("session_id", sessionId)) } }
    }

    private fun supersede(): Int {
        job?.cancel()
        job = null
        // the air dates share the job, so a newer choice drops them like any other call in flight
        _state.update { if (it.airDates == Load.Loading) it.copy(airDates = Load.Idle, order = HitOrder.RELEVANCE) else it }
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

    companion object {
        /** [State.source] when the bridge chose the player. */
        const val AUTO = "auto"

        private fun outcome(reply: BridgeResult): String =
            when (reply) {
                is BridgeResult.Ok -> "ok"
                is BridgeResult.Failure -> "${reply.kind}/${reply.code}"
                BridgeResult.NotStarted -> "not_started"
            }
    }
}
