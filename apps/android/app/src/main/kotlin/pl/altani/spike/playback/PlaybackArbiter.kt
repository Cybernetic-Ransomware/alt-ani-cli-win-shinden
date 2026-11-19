package pl.altani.spike.playback

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** One playback flow at a time: a new one cancels the last, and switching modes releases the old mode's player first. */
class PlaybackArbiter<M : Any>(private val scope: CoroutineScope, private val release: (M) -> Unit) {
    inner class Ticket internal constructor(val mode: M, private val generation: Int) {
        /** False once another flow started; a reply that outlived its cancellation must not start a player. */
        val current: Boolean
            get() = generation == this@PlaybackArbiter.generation
    }

    private val _mode = MutableStateFlow<M?>(null)
    val mode: StateFlow<M?> = _mode.asStateFlow()

    private var job: Job? = null
    private var generation = 0

    fun launch(mode: M, body: suspend (Ticket) -> Unit): Job {
        job?.cancel()
        val ticket = Ticket(mode, ++generation)
        _mode.value?.takeIf { it != mode }?.let(release)
        _mode.value = mode
        return scope.launch { body(ticket) }.also { job = it }
    }
}
