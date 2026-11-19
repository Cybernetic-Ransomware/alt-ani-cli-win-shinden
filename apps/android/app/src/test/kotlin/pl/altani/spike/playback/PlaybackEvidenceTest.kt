package pl.altani.spike.playback

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackEvidenceTest {
    private val evidence = PlaybackEvidence(requiredProgressMs = 3_000)

    private fun play(fromMs: Long, toMs: Long) {
        var at = fromMs
        while (at <= toMs) {
            evidence.onPosition(at, playing = true)
            at += 500
        }
    }

    @Test
    fun `video needs both progress and a rendered frame`() {
        evidence.onTracks(hasVideo = true)
        play(10_000, 14_000)
        assertFalse("progress without a frame", evidence.snapshot.value.confirmed)

        evidence.onFirstFrame()
        assertTrue(evidence.snapshot.value.confirmed)
        assertEquals(4_000, evidence.snapshot.value.progressedMs)
    }

    @Test
    fun `a frame without progress is not playback`() {
        evidence.onTracks(hasVideo = true)
        evidence.onFirstFrame()
        play(0, 1_000)
        assertFalse(evidence.snapshot.value.confirmed)
    }

    @Test
    fun `audio only needs progress, known tracks and audio actually advancing`() {
        play(0, 4_000)
        assertFalse("tracks unknown", evidence.snapshot.value.confirmed)
        evidence.onTracks(hasVideo = false)
        assertFalse("position alone, no audio output", evidence.snapshot.value.confirmed)
        evidence.onAudioAdvancing()
        assertTrue(evidence.snapshot.value.confirmed)
    }

    @Test
    fun `paused samples and seeks add no progress`() {
        evidence.onTracks(hasVideo = true)
        evidence.onFirstFrame()
        play(0, 1_000)
        evidence.onPosition(9_000, playing = false)
        evidence.onSeek(60_000)
        play(60_000, 61_000)
        assertEquals(2_000, evidence.snapshot.value.progressedMs)
        assertFalse(evidence.snapshot.value.confirmed)
    }

    @Test
    fun `each attempt starts from nothing`() {
        evidence.onTracks(hasVideo = true)
        evidence.onFirstFrame()
        play(0, 5_000)
        assertTrue(evidence.snapshot.value.confirmed)

        evidence.reset()

        val fresh = evidence.snapshot.value
        assertEquals(1, fresh.attempt)
        assertFalse(fresh.confirmed || fresh.firstFrame || fresh.tracksKnown)
        assertEquals(0, fresh.progressedMs)
        play(20_000, 21_000)
        assertEquals("counted from the new attempt's first position", 1_000, evidence.snapshot.value.progressedMs)
    }
}
