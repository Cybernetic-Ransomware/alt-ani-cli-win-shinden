package pl.altani.spike.ui

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.LateResult
import pl.altani.spike.playback.WatchSaved

/** search → series → episodes → sources on one coroutine at a time; a newer choice cancels the older call and drops its reply. */
class CatalogFlow(
    private val scope: CoroutineScope,
    private val call: suspend (method: String, args: JSONObject) -> BridgeResult,
    private val log: (String) -> Unit = {},
    private val clock: () -> Long = System::currentTimeMillis,
    lateResults: Flow<LateResult> = emptyFlow(),
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
        val series: SeriesRef? = null,
        val episodes: Load<EpisodePage> = Load.Idle,
        val episode: Episode? = null,
        val sources: Load<SourcePage> = Load.Idle,
        val source: String? = null,
        val order: HitOrder = HitOrder.RELEVANCE,
        val airDates: Load<Map<String, AirDate?>> = Load.Idle,
        // the last reads of the engine's history, on jobs of their own: they never cancel or block the path above
        val recent: Load<List<RecentSeries>> = Load.Idle,
        val watch: Load<SeriesWatch> = Load.Idle,
        // a re-read that failed while the last good read stays on screen, so the user still sees it and can retry
        val recentError: Load.Failed? = null,
        val watchError: Load.Failed? = null,
        // the next pick of [episode] ignores the saved position; chosen by the user, consumed by that pick
        val fromStart: Boolean = false,
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

        /** The last read state of [episode]; display only, `playback_prepare` reads the position itself. */
        val episodeWatch: EpisodeWatch?
            get() = episode?.let { (watch as? Load.Ready)?.value?.episodes?.get(it.number) }
    }

    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state.asStateFlow()

    private var job: Job? = null

    // bumped by every new choice, so a reply that came back despite its cancellation is still dropped
    private var generation = 0

    // episode sessions held in the engine: the one listed on screen and the one the player was given
    private var shown: String? = null
    private var playing: String? = null

    // a write landing while a read is in flight makes that reply stale: it is dropped and the read is made again
    private var recentJob: Job? = null
    private var recentStale = false
    private var watchJob: Job? = null
    private var watchGeneration = 0
    private var watchStale = false

    init {
        // a cancelled open_episode can still open its session in Python; its id then arrives only here
        scope.launch(start = CoroutineStart.UNDISPATCHED) {
            lateResults.collect { late ->
                if (late.method != "open_episode") return@collect
                val id = (late.result as? BridgeResult.Ok)?.data?.optString("session_id")?.ifEmpty { null } ?: return@collect
                if (id == shown || id == playing) return@collect
                log("late open_episode session closed")
                close(id)
            }
        }
    }

    fun search(query: String) {
        val text = query.trim()
        if (text.isEmpty()) return
        val started = supersede()
        show(null)
        if (_state.value.series != null) leaveSeries()
        _state.value = State(query = text, search = Load.Loading, recent = _state.value.recent)
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

    /** The one way into a series, from a search hit ([SeriesHit.ref]) or from the history. */
    fun selectSeries(series: SeriesRef) {
        val started = supersede()
        show(null)
        leaveSeries()
        _state.update {
            it.copy(
                series = series,
                episodes = Load.Loading,
                episode = null,
                sources = Load.Idle,
                source = null,
                watch = Load.Loading,
                watchError = null,
                fromStart = false,
            )
        }
        readWatch(series.id)
        job = scope.launch {
            val reply = timed("episodes") { call("episodes", JSONObject().put("series", series.toJson())) }
            if (started != generation) return@launch
            val load = reply.toLoad(EpisodePage::parse)
            if (load is Load.Ready) log("episodes count=${load.value.episodes.size} complete=${load.value.incompleteReason == null}")
            _state.update { it.copy(episodes = load) }
        }
    }

    fun openEpisode(episode: Episode, fromStart: Boolean = false) {
        val page = (_state.value.episodes as? Load.Ready)?.value ?: return
        if (episode !in page.episodes) return
        val started = supersede()
        show(null)
        _state.update { it.copy(episode = episode, sources = Load.Loading, source = null, fromStart = fromStart) }
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
        _state.update { it.copy(source = source?.onlineId ?: AUTO, fromStart = false) }
        return PlaybackPick(series, page.episode, page.sessionId, source?.onlineId, current.fromStart)
    }

    /** The last opened episode, through the sources screen; nothing when it is not on the current listing. */
    fun resume(fromStart: Boolean) {
        val current = _state.value
        val last = (current.watch as? Load.Ready)?.value?.lastOpened ?: return
        val page = (current.episodes as? Load.Ready)?.value ?: return
        val episode = page.episodes.firstOrNull { it.number == last.number } ?: return
        openEpisode(episode, fromStart)
    }

    /** Shown on the sources screen before any pick, so starting over is always the user's visible choice. */
    fun startFrom(beginning: Boolean) {
        if (_state.value.episode == null) return
        _state.update { it.copy(fromStart = beginning) }
    }

    /** The catalogue is on screen (first show, rotation, back from Home): reads the history unless a read is already going. */
    fun refreshRecent() {
        if (recentJob?.isActive == true) return
        readRecent()
    }

    fun refreshWatch() {
        val series = _state.value.series ?: return
        if (watchJob?.isActive == true) return
        readWatch(series.id)
    }

    /** An accepted write of any viewing, matched by its own series: a late write of an earlier viewing never lands on another one. */
    fun onWatchSaved(saved: WatchSaved) {
        if (saved.periodic) return
        if (recentJob?.isActive == true) recentStale = true else if (_state.value.series == null) readRecent()
        if (_state.value.series?.id != saved.seriesId) return
        invalidateWatch(saved.seriesId)
    }

    fun clearSearch() {
        if (_state.value.series != null) return
        supersede()
        _state.update { State(recent = it.recent) }
        refreshRecent()
    }

    /** The bridge closed a session (network change, access change); a listed one is opened again once. */
    fun onSessionClosed(sessionId: String) {
        if (playing == sessionId) playing = null
        if (shown != sessionId) return
        shown = null
        log("episode session closed, reopening")
        reloadSources()
    }

    fun reloadSources() {
        val current = _state.value
        current.episode?.let { openEpisode(it, current.fromStart) }
    }

    fun back() {
        supersede()
        show(null)
        val series = _state.value.series
        if (_state.value.episode != null) {
            _state.update { it.copy(episode = null, sources = Load.Idle, source = null, fromStart = false) }
            // a playback_begin moved the last opening without any accepted write, so no save signal came for it
            series?.let { invalidateWatch(it.id) }
        } else if (_state.value.series != null) {
            leaveSeries()
            _state.update { it.copy(series = null, episodes = Load.Idle, watch = Load.Idle, watchError = null) }
            refreshRecent()
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
            current.episode != null && current.sources is Load.Failed -> openEpisode(current.episode, current.fromStart)
            series != null && current.episodes is Load.Failed -> selectSeries(series)
            current.search is Load.Failed -> search(current.query)
        }
    }

    private fun readRecent() {
        recentStale = false
        if (_state.value.recent !is Load.Ready) _state.update { it.copy(recent = Load.Loading) }
        recentJob = scope.launch {
            while (true) {
                val reply = timed("watch_recent") { call("watch_recent", JSONObject().put("limit", RECENT_LIMIT)) }
                if (recentStale) {
                    recentStale = false
                    continue
                }
                val load = reply.toLoad(RecentSeries::parsePage)
                if (load is Load.Ready) log("watch_recent series=${load.value.size}")
                _state.update {
                    if (load is Load.Failed && it.recent is Load.Ready) it.copy(recentError = load) else it.copy(recent = load, recentError = null)
                }
                return@launch
            }
        }
    }

    private fun readWatch(seriesId: String) {
        watchJob?.cancel()
        watchStale = false
        val started = ++watchGeneration
        watchJob = scope.launch {
            while (true) {
                val reply = timed("watch_series") { call("watch_series", JSONObject().put("series_id", seriesId)) }
                if (started != watchGeneration || _state.value.series?.id != seriesId) return@launch
                if (watchStale) {
                    watchStale = false
                    continue
                }
                val load = reply.toLoad { SeriesWatch.parse(seriesId, it) }
                if (load is Load.Ready) log("watch_series rows=${load.value.episodes.size} last=${load.value.lastOpened != null}")
                _state.update {
                    if (load is Load.Failed && it.watch is Load.Ready) it.copy(watchError = load) else it.copy(watch = load, watchError = null)
                }
                return@launch
            }
        }
    }

    private fun invalidateWatch(seriesId: String) {
        if (watchJob?.isActive == true) watchStale = true else readWatch(seriesId)
    }

    // the history of the series being left must not land on the next one
    private fun leaveSeries() {
        watchJob?.cancel()
        watchJob = null
        watchStale = false
        watchGeneration++
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

        const val RECENT_LIMIT = 20

        private fun outcome(reply: BridgeResult): String =
            when (reply) {
                is BridgeResult.Ok -> "ok"
                is BridgeResult.Failure -> "${reply.kind}/${reply.code}"
                BridgeResult.NotStarted -> "not_started"
            }
    }
}
