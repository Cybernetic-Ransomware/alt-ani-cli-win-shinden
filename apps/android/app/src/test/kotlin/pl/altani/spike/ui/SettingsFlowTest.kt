package pl.altani.spike.ui

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.LateResult
import pl.altani.spike.ui.FakeCalls.Companion.failure
import pl.altani.spike.ui.FakeCalls.Companion.preferences
import pl.altani.spike.ui.SettingsFlow.SaveResult

@OptIn(ExperimentalCoroutinesApi::class)
class SettingsFlowTest {
    private val fake = FakeCalls()

    private fun TestScope.flow() = SettingsFlow(backgroundScope, fake::call)

    private fun count(method: String) = fake.calls.count { it.method == method }

    private fun ok(json: JSONObject) = BridgeResult.Ok(json)

    private fun conflict(expected: Long, found: Long) =
        BridgeResult.Failure("state.conflict", "conflict", false, JSONObject().put("expected", expected).put("found", found))

    private fun changed(revision: Long) = JSONObject().put("type", "preferences.changed").put("data", JSONObject().put("revision", revision))

    private fun TestScope.loaded(flow: SettingsFlow, json: JSONObject = preferences(1)): SettingsFlow {
        flow.refresh()
        runCurrent()
        fake.last(SettingsFlow.GET).answer(ok(json))
        runCurrent()
        return flow
    }

    private fun TestScope.answerGet(json: JSONObject) {
        fake.last(SettingsFlow.GET).answer(ok(json))
        runCurrent()
    }

    private fun TestScope.answerUpdate(result: BridgeResult) {
        fake.last(SettingsFlow.UPDATE).answer(result)
        runCurrent()
    }

    private fun SettingsFlow.form() = state.value.form!!

    @Test
    fun `the first read shows loading, then exactly the engine's values`() = runTest {
        val flow = flow()
        flow.refresh()
        runCurrent()
        assertTrue(flow.state.value.loading)
        assertNull(flow.state.value.form)
        answerGet(preferences(4, audio = "jp", subs = "none", quality = "max:900", network = "any", alternatives = true, active = 2))
        val s = flow.state.value
        assertFalse(s.loading)
        assertEquals(4L, s.base!!.revision)
        assertEquals(LanguageMode.SPECIFIC, s.form!!.audioMode)
        assertEquals("jp", s.form.audioText)
        assertEquals(SubsMode.NONE, s.form.subsMode)
        assertEquals("900", s.form.heightText)
        assertTrue(s.dirty.isEmpty())
        assertFalse(s.canSave)
    }

    @Test
    fun `a failed first read can be retried`() = runTest {
        val flow = flow()
        flow.refresh()
        runCurrent()
        fake.last(SettingsFlow.GET).answer(failure("internal", "internal"))
        runCurrent()
        assertEquals("internal", flow.state.value.loadError!!.code)
        assertNull(flow.state.value.form)
        loaded(flow)
        assertNull(flow.state.value.loadError)
        assertEquals(1L, flow.state.value.base!!.revision)
    }

    @Test
    fun `a malformed read is an error, not a set of defaults`() = runTest {
        val flow = flow()
        loaded(flow, preferences(1).put("auto_alternatives", "false"))
        assertEquals("malformed_reply", flow.state.value.loadError!!.code)
        assertNull(flow.state.value.base)
    }

    @Test
    fun `a failed re-read keeps the shown preferences`() = runTest {
        val flow = loaded(flow())
        flow.refresh()
        runCurrent()
        fake.last(SettingsFlow.GET).answer(failure("timeout"))
        runCurrent()
        assertEquals(1L, flow.state.value.base!!.revision)
        assertEquals("timeout", flow.state.value.loadError!!.code)
    }

    @Test
    fun `editing, refreshing and reading the state never write`() = runTest {
        val flow = loaded(flow())
        repeat(5) { i -> flow.edit { it.copy(maxActiveDownloads = 1 + i) } }
        flow.edit { it.copy(audioMode = LanguageMode.SPECIFIC, audioText = "j") }
        flow.edit { it.copy(audioText = "jp") }
        repeat(10) { flow.state.value.canSave }
        flow.refresh()
        runCurrent()
        answerGet(preferences(1))
        assertEquals(0, count(SettingsFlow.UPDATE))
        assertTrue(flow.state.value.canSave)
    }

    @Test
    fun `an empty diff never calls the bridge`() = runTest {
        val flow = loaded(flow())
        flow.save()
        flow.edit { it.copy(autoAlternatives = true) }
        flow.edit { it.copy(autoAlternatives = false) }
        flow.save()
        runCurrent()
        assertEquals(0, count(SettingsFlow.UPDATE))
    }

    @Test
    fun `an invalid draft cannot be saved`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(qualityMode = QualityMode.MAX, heightText = "0") }
        assertEquals(setOf(PreferenceField.QUALITY), flow.state.value.problems)
        flow.save()
        runCurrent()
        assertEquals(0, count(SettingsFlow.UPDATE))
    }

    @Test
    fun `save sends only the changed fields with the base revision`() = runTest {
        val flow = loaded(flow(), preferences(3))
        flow.edit { it.copy(subsMode = SubsMode.NONE, maxActiveDownloads = 2) }
        flow.save()
        runCurrent()
        val args = fake.last(SettingsFlow.UPDATE).args
        assertEquals(3L, args.getLong("expected_revision"))
        val changes = args.getJSONObject("changes")
        assertEquals(setOf("subs", "max_active_downloads"), changes.keyNames())
        assertEquals("none", changes.getString("subs"))
        assertEquals(2, changes.getInt("max_active_downloads"))
    }

    @Test
    fun `a successful save applies the new revision and clears the draft`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(audioMode = LanguageMode.SPECIFIC, audioText = "JP") }
        flow.save()
        runCurrent()
        assertTrue(flow.state.value.saving)
        answerUpdate(ok(preferences(2, audio = "jp")))
        val s = flow.state.value
        assertFalse(s.saving)
        assertEquals(2L, s.base!!.revision)
        assertEquals(SaveResult.Saved, s.result)
        assertEquals("jp", s.form!!.audioText)
        assertTrue(s.dirty.isEmpty())
        assertEquals("a confirmed save needs no read", 1, count(SettingsFlow.GET))
    }

    @Test
    fun `edits and a second save are ignored while a save runs`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(autoAlternatives = true) }
        flow.save()
        runCurrent()
        flow.save()
        flow.edit { it.copy(maxActiveDownloads = 5) }
        runCurrent()
        assertEquals(1, count(SettingsFlow.UPDATE))
        assertEquals(1, flow.form().maxActiveDownloads)
    }

    @Test
    fun `two quick saves chain revisions and a late older read is dropped`() = runTest {
        val flow = loaded(flow())
        flow.refresh()
        runCurrent()
        val lateRead = fake.last(SettingsFlow.GET)
        flow.edit { it.copy(autoAlternatives = true) }
        flow.save()
        runCurrent()
        answerUpdate(ok(preferences(2, alternatives = true)))
        flow.edit { it.copy(maxActiveDownloads = 3) }
        flow.save()
        runCurrent()
        assertEquals(2L, fake.last(SettingsFlow.UPDATE).args.getLong("expected_revision"))
        assertEquals(setOf("max_active_downloads"), fake.last(SettingsFlow.UPDATE).args.getJSONObject("changes").keyNames())
        answerUpdate(ok(preferences(3, alternatives = true, active = 3)))
        lateRead.answer(ok(preferences(1)))
        runCurrent()
        val s = flow.state.value
        assertEquals(3L, s.base!!.revision)
        assertTrue(s.form!!.autoAlternatives)
        assertEquals(3, s.form.maxActiveDownloads)
        assertEquals(SaveResult.Saved, s.result)
    }

    @Test
    fun `a conflict on a field the user did not edit keeps the draft and needs another Save`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(audioMode = LanguageMode.SPECIFIC, audioText = "jp") }
        flow.save()
        runCurrent()
        answerUpdate(conflict(1, 2))
        assertTrue(flow.state.value.saving)
        answerGet(preferences(2, active = 4))
        val s = flow.state.value
        assertFalse(s.saving)
        assertEquals(SaveResult.Rebased, s.result)
        assertTrue(s.conflicts.isEmpty())
        assertEquals("jp", s.form!!.audioText)
        assertEquals(4, s.form.maxActiveDownloads)
        assertEquals(1, count(SettingsFlow.UPDATE))
        flow.save()
        runCurrent()
        val args = fake.last(SettingsFlow.UPDATE).args
        assertEquals(2L, args.getLong("expected_revision"))
        assertEquals(setOf("audio"), args.getJSONObject("changes").keyNames())
    }

    @Test
    fun `a conflict on the same field to another value waits for the user`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(qualityMode = QualityMode.MAX, heightText = "720", autoAlternatives = true) }
        flow.save()
        runCurrent()
        answerUpdate(conflict(1, 2))
        answerGet(preferences(2, quality = "max:480", network = "any"))
        val s = flow.state.value
        assertEquals(listOf(PreferenceField.QUALITY), s.conflicts.map { it.field })
        assertEquals("At most 720p", s.conflicts.single().mine)
        assertEquals("At most 480p", s.conflicts.single().current)
        assertEquals(DownloadNetwork.ANY, s.form!!.downloadNetwork)
        assertFalse(s.canSave)
        flow.save()
        runCurrent()
        assertEquals(1, count(SettingsFlow.UPDATE))

        flow.resolve(keepMine = true)
        runCurrent()
        val args = fake.last(SettingsFlow.UPDATE).args
        assertEquals(2, count(SettingsFlow.UPDATE))
        assertEquals(2L, args.getLong("expected_revision"))
        // the network changed elsewhere is not written back
        assertEquals(setOf("quality", "auto_alternatives"), args.getJSONObject("changes").keyNames())
        assertEquals("max:720", args.getJSONObject("changes").getString("quality"))
    }

    @Test
    fun `use current takes the engine's value for the clashing fields only`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(qualityMode = QualityMode.MAX, heightText = "720", autoAlternatives = true) }
        flow.save()
        runCurrent()
        answerUpdate(conflict(1, 2))
        answerGet(preferences(2, quality = "max:480"))
        flow.resolve(keepMine = false)
        val s = flow.state.value
        assertTrue(s.conflicts.isEmpty())
        assertEquals("480", s.form!!.heightText)
        assertEquals(setOf(PreferenceField.AUTO_ALTERNATIVES), s.dirty)
        assertEquals(1, count(SettingsFlow.UPDATE))
    }

    @Test
    fun `the same value written elsewhere is no clash`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(subsMode = SubsMode.SPECIFIC, subsText = "pl") }
        flow.save()
        runCurrent()
        answerUpdate(conflict(1, 2))
        answerGet(preferences(2, subs = "pl"))
        val s = flow.state.value
        assertTrue(s.conflicts.isEmpty())
        assertEquals(SaveResult.Saved, s.result)
        assertTrue(s.dirty.isEmpty())
        assertEquals(1, count(SettingsFlow.UPDATE))
    }

    @Test
    fun `a conflict stays shown when a later snapshot does not settle it`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(maxActiveDownloads = 3) }
        flow.save()
        runCurrent()
        answerUpdate(conflict(1, 2))
        answerGet(preferences(2, active = 5))
        flow.onEvent(changed(3))
        runCurrent()
        answerGet(preferences(3, active = 5, alternatives = true))
        val s = flow.state.value
        assertEquals(listOf(PreferenceField.MAX_ACTIVE_DOWNLOADS), s.conflicts.map { it.field })
        assertTrue(s.form!!.autoAlternatives)
        assertEquals(3, s.form.maxActiveDownloads)
        // the engine reaching the user's value settles it
        flow.onEvent(changed(4))
        runCurrent()
        answerGet(preferences(4, active = 3, alternatives = true))
        assertTrue(flow.state.value.conflicts.isEmpty())
        assertTrue(flow.state.value.dirty.isEmpty())
    }

    @Test
    fun `an uncertain failure re-reads and keeps the draft when nothing was written`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(downloadNetwork = DownloadNetwork.ANY) }
        flow.save()
        runCurrent()
        answerUpdate(failure("internal", "internal"))
        assertTrue("the outcome is checked before another save", flow.state.value.saving)
        answerGet(preferences(1))
        val s = flow.state.value
        assertFalse(s.saving)
        assertEquals("internal", (s.result as SaveResult.Failed).code)
        assertEquals(DownloadNetwork.ANY, s.form!!.downloadNetwork)
        assertTrue(s.canSave)
        flow.save()
        runCurrent()
        assertEquals(1L, fake.last(SettingsFlow.UPDATE).args.getLong("expected_revision"))
    }

    @Test
    fun `an uncertain failure whose write landed counts as saved`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(downloadNetwork = DownloadNetwork.ANY) }
        flow.save()
        runCurrent()
        answerUpdate(BridgeResult.Failure("cancelled", "cancelled", false, JSONObject()))
        answerGet(preferences(2, network = "any"))
        assertEquals(SaveResult.Saved, flow.state.value.result)
        assertTrue(flow.state.value.dirty.isEmpty())
        assertEquals(1, count(SettingsFlow.UPDATE))
    }

    @Test
    fun `a conflict whose read fails blocks Save until a retried read succeeds`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(qualityMode = QualityMode.MAX, heightText = "720") }
        flow.save()
        runCurrent()
        answerUpdate(conflict(1, 2))
        fake.last(SettingsFlow.GET).answer(failure("timeout"))
        runCurrent()
        val blocked = flow.state.value
        assertFalse(blocked.saving)
        assertEquals(SaveResult.Rebased, blocked.unverified)
        assertNull(blocked.result)
        assertFalse(blocked.canSave)
        assertEquals("720", blocked.form!!.heightText)
        assertEquals(1L, blocked.base!!.revision)
        flow.save()
        runCurrent()
        assertEquals(1, count(SettingsFlow.UPDATE))

        flow.retryVerification()
        runCurrent()
        assertTrue(flow.state.value.saving)
        answerGet(preferences(2, active = 4))
        val s = flow.state.value
        assertNull(s.unverified)
        assertEquals(SaveResult.Rebased, s.result)
        assertEquals(2L, s.base!!.revision)
        assertEquals(4, s.form!!.maxActiveDownloads)
        assertTrue(s.canSave)
        assertEquals(1, count(SettingsFlow.UPDATE))
    }

    @Test
    fun `an unknown outcome whose read is malformed waits for a retried read to confirm the save`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(downloadNetwork = DownloadNetwork.ANY) }
        flow.save()
        runCurrent()
        answerUpdate(failure("internal", "internal"))
        answerGet(preferences(2, network = "any").put("revision", 2.5))
        val blocked = flow.state.value
        assertTrue(blocked.unverified is SaveResult.Failed)
        assertEquals("malformed_reply", blocked.loadError!!.code)
        assertFalse(blocked.canSave)

        flow.retryVerification()
        runCurrent()
        answerGet(preferences(2, network = "any"))
        val s = flow.state.value
        assertNull(s.unverified)
        assertNull(s.loadError)
        assertEquals(SaveResult.Saved, s.result)
        assertTrue(s.dirty.isEmpty())
        assertEquals(1, count(SettingsFlow.UPDATE))
    }

    @Test
    fun `a successful save clears an earlier refresh error`() = runTest {
        val flow = loaded(flow())
        flow.refresh()
        runCurrent()
        fake.last(SettingsFlow.GET).answer(failure("timeout"))
        runCurrent()
        assertEquals("timeout", flow.state.value.loadError!!.code)
        flow.edit { it.copy(autoAlternatives = true) }
        flow.save()
        runCurrent()
        answerUpdate(ok(preferences(2, alternatives = true)))
        assertNull(flow.state.value.loadError)
        assertEquals(SaveResult.Saved, flow.state.value.result)
    }

    @Test
    fun `a malformed save reply is checked by a read`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(autoAlternatives = true) }
        flow.save()
        runCurrent()
        answerUpdate(ok(preferences(2, alternatives = true).put("revision", "2")))
        answerGet(preferences(2, alternatives = true))
        assertEquals(SaveResult.Saved, flow.state.value.result)
        assertEquals(2L, flow.state.value.base!!.revision)
    }

    @Test
    fun `a refused request is a failure without a read`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(autoAlternatives = true) }
        flow.save()
        runCurrent()
        answerUpdate(BridgeResult.Failure("bad_request", "invalid_input", false, JSONObject().put("problem", "invalid_preferences")))
        assertEquals(1, count(SettingsFlow.GET))
        assertFalse(flow.state.value.saving)
        assertEquals("bad_request", (flow.state.value.result as SaveResult.Failed).code)
        assertTrue(flow.form().autoAlternatives)
    }

    @Test
    fun `a change elsewhere to a field not edited updates it and keeps a half-typed draft`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(audioMode = LanguageMode.SPECIFIC, audioText = "") }
        flow.edit { it.copy(qualityMode = QualityMode.MAX, heightText = "10") }
        flow.onEvent(changed(2))
        runCurrent()
        answerGet(preferences(2, network = "any"))
        val s = flow.state.value
        assertEquals(2L, s.base!!.revision)
        assertTrue(s.conflicts.isEmpty())
        assertEquals(LanguageMode.SPECIFIC, s.form!!.audioMode)
        assertEquals("", s.form.audioText)
        assertEquals("10", s.form.heightText)
        assertEquals(DownloadNetwork.ANY, s.form.downloadNetwork)
        assertEquals(setOf(PreferenceField.AUDIO, PreferenceField.QUALITY), s.dirty)
        assertEquals(setOf(PreferenceField.AUDIO), s.problems)
    }

    @Test
    fun `a change elsewhere to an edited field is a conflict, to the same value it is not`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(maxActiveDownloads = 2, autoAlternatives = true) }
        flow.onEvent(changed(2))
        runCurrent()
        answerGet(preferences(2, active = 3, alternatives = true))
        val s = flow.state.value
        assertEquals(listOf(PreferenceField.MAX_ACTIVE_DOWNLOADS), s.conflicts.map { it.field })
        assertEquals(setOf(PreferenceField.MAX_ACTIVE_DOWNLOADS), s.dirty)
    }

    @Test
    fun `picking the current value by hand settles the conflict`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(maxActiveDownloads = 2) }
        flow.onEvent(changed(2))
        runCurrent()
        answerGet(preferences(2, active = 3))
        flow.edit { it.copy(maxActiveDownloads = 3) }
        assertTrue(flow.state.value.conflicts.isEmpty())
        assertTrue(flow.state.value.dirty.isEmpty())
    }

    @Test
    fun `a change elsewhere with no draft refreshes the view`() = runTest {
        val flow = loaded(flow())
        flow.onEvent(changed(2))
        runCurrent()
        answerGet(preferences(2, audio = "jp"))
        assertEquals("jp", flow.form().audioText)
        assertTrue(flow.state.value.dirty.isEmpty())
    }

    @Test
    fun `a signal not newer than the held revision does not read`() = runTest {
        val flow = loaded(flow(), preferences(5))
        flow.onEvent(changed(5))
        flow.onEvent(changed(4))
        flow.onEvent(JSONObject().put("type", "queue.changed").put("data", JSONObject().put("queue_revision", 9)))
        runCurrent()
        assertEquals(1, count(SettingsFlow.GET))
    }

    @Test
    fun `the save's own signal during the save does not reset anything`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(autoAlternatives = true) }
        flow.save()
        runCurrent()
        flow.onEvent(changed(2))
        runCurrent()
        answerGet(preferences(2, alternatives = true))
        answerUpdate(ok(preferences(2, alternatives = true)))
        assertEquals(SaveResult.Saved, flow.state.value.result)
        // a new edit after the save is not undone by the same signal arriving again
        flow.edit { it.copy(maxActiveDownloads = 4) }
        flow.onEvent(changed(2))
        runCurrent()
        assertEquals(2, count(SettingsFlow.GET))
        assertEquals(4, flow.form().maxActiveDownloads)
    }

    @Test
    fun `refreshes asked for during a read coalesce into one more read`() = runTest {
        val flow = loaded(flow())
        flow.refresh()
        runCurrent()
        flow.refresh()
        flow.refresh()
        runCurrent()
        assertEquals(2, count(SettingsFlow.GET))
        answerGet(preferences(1))
        assertEquals(3, count(SettingsFlow.GET))
        answerGet(preferences(2))
        assertEquals(3, count(SettingsFlow.GET))
        assertEquals(2L, flow.state.value.base!!.revision)
        assertFalse(flow.state.value.loading)
    }

    @Test
    fun `a resync re-read keeps the draft when nothing changed`() = runTest {
        val flow = loaded(flow())
        flow.edit { it.copy(subsMode = SubsMode.SPECIFIC, subsText = "p") }
        flow.refresh()
        runCurrent()
        answerGet(preferences(1))
        assertEquals("p", flow.form().subsText)
        assertEquals(SubsMode.SPECIFIC, flow.form().subsMode)
    }

    @Test
    fun `cancel restores the engine's values`() = runTest {
        val flow = loaded(flow(), preferences(1, quality = "max:1080"))
        flow.edit { it.copy(qualityMode = QualityMode.BEST, autoAlternatives = true) }
        flow.cancel()
        assertTrue(flow.state.value.dirty.isEmpty())
        assertEquals(QualityMode.MAX, flow.form().qualityMode)
        assertEquals("1080", flow.form().heightText)
    }

    @Test
    fun `a late save result re-reads, other late results do not`() = runTest {
        val flow = loaded(flow())
        flow.onLateResult(LateResult("a", "watch_set_watched", true, ok(JSONObject())))
        flow.onLateResult(LateResult("b", SettingsFlow.UPDATE, true, BridgeResult.NotStarted))
        flow.onLateResult(LateResult("c", SettingsFlow.GET, false, ok(preferences(1))))
        runCurrent()
        assertEquals(1, count(SettingsFlow.GET))
        flow.onLateResult(LateResult("d", SettingsFlow.UPDATE, true, ok(preferences(2))))
        runCurrent()
        assertEquals(2, count(SettingsFlow.GET))
    }
}
