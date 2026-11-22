package pl.altani.spike.ui

import org.json.JSONException
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.ui.FakeCalls.Companion.preferences

class SettingsModelsTest {
    private fun parse(json: JSONObject) = Preferences.parse(json)

    private fun rejects(json: JSONObject) {
        assertThrows(JSONException::class.java) { parse(json) }
    }

    private val defaults = PreferenceValues()

    @Test
    fun `a full DTO parses into the six values and the revision`() {
        val prefs = parse(preferences(7, audio = "jp", subs = "none", quality = "max:720", network = "any", alternatives = true, active = 3))
        assertEquals(7L, prefs.revision)
        assertEquals(PreferenceValues("jp", "none", Quality.Max(720), DownloadNetwork.ANY, true, 3), prefs.values)
    }

    @Test
    fun `the engine's defaults parse into the default values`() {
        val prefs = parse(preferences())
        assertEquals(0L, prefs.revision)
        assertEquals(PreferenceValues("any", "any", Quality.Best, DownloadNetwork.WIFI_ONLY, false, 1), prefs.values)
        assertEquals(defaults, prefs.values)
    }

    @Test
    fun `a stored count above the bridge's input limit is still shown`() {
        assertEquals(20, parse(preferences(active = 20)).values.maxActiveDownloads)
    }

    @Test
    fun `every required key must be present and not null`() {
        for (key in listOf("audio", "subs", "quality", "download_network", "auto_alternatives", "max_active_downloads", "revision")) {
            rejects(preferences().apply { remove(key) })
            rejects(preferences().put(key, JSONObject.NULL))
        }
    }

    @Test
    fun `a value of another JSON type is refused, never coerced`() {
        rejects(preferences().put("auto_alternatives", "true"))
        rejects(preferences().put("auto_alternatives", 1))
        rejects(preferences().put("revision", "3"))
        rejects(preferences().put("revision", 3.0))
        rejects(preferences().put("revision", 3.5))
        rejects(preferences().put("max_active_downloads", 2.0))
        rejects(preferences().put("max_active_downloads", "2"))
        rejects(preferences().put("max_active_downloads", Long.MAX_VALUE))
        rejects(preferences().put("audio", 1))
        rejects(preferences().put("subs", false))
        rejects(preferences().put("quality", 720))
        rejects(preferences().put("download_network", JSONObject()))
    }

    @Test
    fun `a JSON text with fractions is refused the same way as built values`() {
        rejects(JSONObject(preferences().toString().replace("\"revision\":0", "\"revision\":0.0")))
        assertEquals(0L, parse(JSONObject(preferences().toString())).revision)
    }

    @Test
    fun `unknown enums, bad quality encodings and out of range values are refused`() {
        rejects(preferences(network = "cellular"))
        rejects(preferences(network = "WIFI_ONLY"))
        for (quality in listOf("max:0", "max:abc", "720p", "max:", "max:0720", "max:100000", "Best", "")) {
            rejects(preferences(quality = quality))
        }
        rejects(preferences(active = 0))
        rejects(preferences(active = -1))
        rejects(preferences(revision = -1))
        rejects(preferences(audio = ""))
        rejects(preferences(audio = "  "))
        rejects(preferences(audio = "none"))
        rejects(preferences(subs = ""))
    }

    @Test
    fun `quality round-trips through its encoding, including heights outside the presets`() {
        for (text in listOf("best", "max:1", "max:480", "max:900", "max:99999")) {
            assertEquals(text, Quality.parse(text).encode())
        }
    }

    @Test
    fun `a form made from values has no changes`() {
        val values = PreferenceValues("jp", "pl", Quality.Max(900), DownloadNetwork.ANY, true, 4)
        val form = SettingsForm.from(values)
        assertTrue(form.dirty(values).isEmpty())
        assertEquals(0, form.changes(values)!!.length())
        assertEquals(SubsMode.SPECIFIC, form.subsMode)
        assertEquals("900", form.heightText)
    }

    @Test
    fun `each field is encoded alone when it is the only change`() {
        val form = SettingsForm.from(defaults)
        val cases = listOf(
            form.copy(audioMode = LanguageMode.SPECIFIC, audioText = " jp ") to ("audio" to "jp"),
            form.copy(subsMode = SubsMode.NONE) to ("subs" to "none"),
            form.copy(subsMode = SubsMode.SPECIFIC, subsText = "pl") to ("subs" to "pl"),
            form.copy(qualityMode = QualityMode.MAX, heightText = "720") to ("quality" to "max:720"),
            form.copy(downloadNetwork = DownloadNetwork.ANY) to ("download_network" to "any"),
            form.copy(autoAlternatives = true) to ("auto_alternatives" to true),
            form.copy(maxActiveDownloads = 16) to ("max_active_downloads" to 16),
        )
        for ((edited, expected) in cases) {
            val changes = edited.changes(defaults)!!
            assertEquals(setOf(expected.first), changes.keyNames())
            assertEquals(expected.second, changes.get(expected.first))
        }
    }

    @Test
    fun `a language typed in another case or spacing is not a change`() {
        val values = PreferenceValues(audio = "jp")
        assertTrue(SettingsForm.from(values).copy(audioText = " JP ").dirty(values).isEmpty())
    }

    @Test
    fun `an invalid changed field blocks the whole change set`() {
        val form = SettingsForm.from(defaults).copy(autoAlternatives = true)
        val invalid = listOf(
            form.copy(audioMode = LanguageMode.SPECIFIC, audioText = "") to PreferenceField.AUDIO,
            form.copy(audioMode = LanguageMode.SPECIFIC, audioText = "None") to PreferenceField.AUDIO,
            form.copy(subsMode = SubsMode.SPECIFIC, subsText = "   ") to PreferenceField.SUBS,
            form.copy(qualityMode = QualityMode.MAX, heightText = "") to PreferenceField.QUALITY,
            form.copy(qualityMode = QualityMode.MAX, heightText = "0") to PreferenceField.QUALITY,
            form.copy(qualityMode = QualityMode.MAX, heightText = "100000") to PreferenceField.QUALITY,
            form.copy(maxActiveDownloads = 17) to PreferenceField.MAX_ACTIVE_DOWNLOADS,
            form.copy(maxActiveDownloads = 0) to PreferenceField.MAX_ACTIVE_DOWNLOADS,
        )
        for ((edited, field) in invalid) {
            assertEquals(setOf(field), edited.problems(edited.dirty(defaults)))
            assertNull(edited.changes(defaults))
        }
    }

    @Test
    fun `a stored value outside the input limits does not block saving other fields`() {
        val values = PreferenceValues(maxActiveDownloads = 20)
        val form = SettingsForm.from(values).copy(autoAlternatives = true)
        assertEquals(setOf("auto_alternatives"), form.changes(values)!!.keyNames())
    }
}
