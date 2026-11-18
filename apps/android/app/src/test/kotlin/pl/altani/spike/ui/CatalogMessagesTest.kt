package pl.altani.spike.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CatalogMessagesTest {
    private fun says(code: String, kind: String? = null, vararg words: String) {
        val text = CatalogMessages.describe(code, kind)
        words.forEach { assertTrue("\"$text\" for $code lacks \"$it\"", it in text) }
    }

    @Test
    fun `access, network and missing players are told apart`() {
        says("access_blocked", "access", "blocked")
        says("xhr_access_blocked", null, "blocked")
        says("network.offline", "unavailable", "No network")
        says("timeout", "unavailable", "did not answer", "timeout")
        says("player_not_found", null, "No working player")
        says("sources_exhausted", null, "No working player")
        says("xhr_next_player", null, "No working player")
        says("criteria.no_match", "no_match", "preferences")
    }

    @Test
    fun `media3 and controller codes are explained`() {
        says("player:ERROR_CODE_IO_BAD_HTTP_STATUS", null, "could not play", "ERROR_CODE_IO_BAD_HTTP_STATUS")
        says("refresh_limit", null, "expiring")
        says("parse", null, "unexpected form")
    }

    @Test
    fun `unknown codes fall back on their kind and otherwise on the code alone`() {
        says("xhr_something_new", "unavailable", "unavailable", "xhr_something_new")
        says("brand_new", "access", "blocked")
        assertEquals("Unexpected error. (internal)", CatalogMessages.describe("internal", "internal"))
    }
}
