package pl.altani.spike.playback

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackSourceTest {
    private val secretUrl = "http://127.0.0.1:8765/m/1/SECRETSIG/hls/index.m3u8"

    private fun online(changed: Boolean = false) =
        JSONObject()
            .put("mode", "online")
            .put("session_id", "e1")
            .put("variant_changed", changed)
            .put(
                "variant",
                JSONObject()
                    .put("url", secretUrl)
                    .put("protocol", "hls")
                    .put("mime", "application/x-mpegURL")
                    .put("label", "720p")
                    .put("headers", JSONObject().put("Referer", "https://controlled.test/e/1").put("Origin", "SECRETORIGIN")),
            )

    @Test
    fun `online carries the variant with its headers`() {
        val source = PlaybackSource.parse(online(changed = true)) as PlaybackSource.Online
        assertEquals("e1", source.sessionId)
        assertEquals(secretUrl, source.variant.url)
        assertEquals("SECRETORIGIN", source.variant.headers["Origin"])
        assertTrue(source.variantChanged)
    }

    @Test
    fun `local carries the target kind and value`() {
        val data = JSONObject().put("mode", "local").put("target", JSONObject().put("kind", "content_uri").put("value", "content://a/b"))
        val source = PlaybackSource.parse(data) as PlaybackSource.Local
        assertEquals("content_uri" to "content://a/b", source.kind to source.value)
    }

    @Test
    fun `toString never shows the url or a header value`() {
        val text = PlaybackSource.parse(online()).toString()
        assertFalse(text.contains("SECRET"))
        assertTrue(text.contains("720p"))
    }
}
