package pl.altani.spike.offline.background

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class NotificationModelTest {
    @Test
    fun `one transfer with a known size shows its share`() {
        val model = NotificationModels.session(RunSummary(activeIo = 1, bytesDone = 524_288, bytesTotal = 2_097_152))
        assertEquals(NotificationProgress.Determinate(NotificationModels.PROGRESS_MAX, 250), model.progress)
        assertEquals("0.5 MB of 2.0 MB", model.text)
    }

    @Test
    fun `an unknown size or several transfers are indeterminate`() {
        val unknown = NotificationModels.session(RunSummary(activeIo = 1, bytesDone = 1_048_576))
        assertEquals(NotificationProgress.Indeterminate, unknown.progress)
        assertEquals("1.0 MB downloaded", unknown.text)
        val several = NotificationModels.session(RunSummary(activeIo = 2, readyNow = 1, bytesDone = 0, bytesTotal = 10))
        assertEquals(NotificationProgress.Indeterminate, several.progress)
        assertEquals("2 downloads, 0.0 MB of 0.0 MB · 1 more queued", several.text)
    }

    @Test
    fun `waits say what they wait for and show no bar`() {
        val wifi = NotificationModels.session(RunSummary(waitingNetwork = 1, scope = NetworkScope.WIFI))
        assertEquals("Waiting for Wi-Fi", wifi.text)
        assertNull(wifi.progress)
        assertEquals("Waiting for a network connection", NotificationModels.session(RunSummary(waitingNetwork = 1, scope = NetworkScope.ANY)).text)
        assertEquals(DownloadTexts.RETRYING, NotificationModels.session(RunSummary(deferred = 1)).text)
    }

    @Test
    fun `no model carries anything but counts and sizes`() {
        val texts = AttentionReason.entries.map { NotificationModels.attention(it).text } +
            NotificationModels.session(RunSummary(activeIo = 1, bytesDone = 1, bytesTotal = 2)).text
        assertTrue(texts.all { it.isNotBlank() })
        assertFalse(texts.any { "http" in it || "/" in it })
    }
}
