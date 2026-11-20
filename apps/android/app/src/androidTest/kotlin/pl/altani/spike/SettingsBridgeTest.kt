package pl.altani.spike

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chaquo.python.Python
import java.io.File
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.cancelChildren
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import pl.altani.spike.bridge.BridgeResult
import pl.altani.spike.bridge.BridgeRuntime
import pl.altani.spike.bridge.ChaquopyEndpoint
import pl.altani.spike.ui.DownloadNetwork
import pl.altani.spike.ui.LanguageMode
import pl.altani.spike.ui.PreferenceField
import pl.altani.spike.ui.PreferenceValues
import pl.altani.spike.ui.Quality
import pl.altani.spike.ui.QualityMode
import pl.altani.spike.ui.SettingsFlow
import pl.altani.spike.ui.SubsMode

/** [SettingsFlow] on the real bridge, without changing the contract; each test has its own database. */
@RunWith(AndroidJUnit4::class)
class SettingsBridgeTest {
    private val app = ApplicationProvider.getApplicationContext<AltAniApplication>()
    private val dbFiles = mutableListOf<File>()
    private val runtimes = mutableListOf<BridgeRuntime>()

    private fun newDb() = File(app.filesDir, "settings-${UUID.randomUUID()}.db").also { dbFiles += it }

    private fun runtime(db: File): BridgeRuntime =
        BridgeRuntime(
            endpointFactory = { ChaquopyEndpoint(Python.getInstance().getModule("android_host").callAttr("create_bridge")) },
            engineArgs = {
                JSONObject().put("cache_dir", app.cacheDir.absolutePath).put("history_path", db.absolutePath).put("network", "wifi")
            },
        ).also { runtimes += it }

    // runBlocking's single thread keeps the flow's state on one thread, as the main thread does in the app
    private fun CoroutineScope.flow(runtime: BridgeRuntime) = SettingsFlow(this, { method, args -> runtime.call(method, args, mutation = method == SettingsFlow.UPDATE) })

    private suspend fun SettingsFlow.awaitState(predicate: (SettingsFlow.State) -> Boolean) = withTimeout(20_000) { state.first(predicate) }

    private fun test(body: suspend CoroutineScope.() -> Unit) = runBlocking { withTimeout(90_000) { body() } }

    @After
    fun tearDown() = runBlocking {
        runtimes.forEach { it.shutdown(5_000) }
        dbFiles.forEach { db -> db.parentFile?.listFiles { f -> f.name.startsWith(db.name) }?.forEach { it.delete() } }
    }

    @Test
    fun saved_preferences_survive_a_new_runtime_on_the_same_database() = test {
        val db = newDb()
        val first = runtime(db)
        val flow = flow(first)
        flow.refresh()
        val fresh = flow.awaitState { it.base != null }
        assertEquals(0L, fresh.base!!.revision)
        assertEquals(PreferenceValues(), fresh.base.values)

        flow.edit {
            it.copy(
                audioMode = LanguageMode.SPECIFIC, audioText = " JP ", subsMode = SubsMode.NONE, qualityMode = QualityMode.MAX,
                heightText = "900", downloadNetwork = DownloadNetwork.ANY, autoAlternatives = true, maxActiveDownloads = 3,
            )
        }
        flow.save()
        val saved = flow.awaitState { !it.saving && it.result != null }
        assertEquals(SettingsFlow.SaveResult.Saved, saved.result)
        val expected = PreferenceValues("jp", "none", Quality.Max(900), DownloadNetwork.ANY, true, 3)
        assertEquals(expected, saved.base!!.values)
        assertEquals(1L, saved.base.revision)
        assertTrue(first.shutdown(5_000).ended)

        val reopened = flow(runtime(db))
        reopened.refresh()
        val read = reopened.awaitState { it.base != null }
        assertEquals(expected, read.base!!.values)
        assertEquals(1L, read.base.revision)
    }

    @Test
    fun another_handle_changing_an_unedited_field_reaches_the_draft_through_the_events() = test {
        val db = newDb()
        val mine = runtime(db)
        val other = runtime(db)
        val flow = flow(mine)
        launch { mine.events.events.collect(flow::onEvent) }
        launch { mine.events.resync.collect { flow.refresh() } }
        mine.events.start()
        flow.awaitState { it.base != null }
        flow.edit { it.copy(autoAlternatives = true) }

        val write = other.call(SettingsFlow.UPDATE, JSONObject().put("changes", JSONObject().put("download_network", "any")).put("expected_revision", 0))
        assertTrue(write is BridgeResult.Ok)

        val merged = flow.awaitState { (it.base?.revision ?: 0) >= 1 }
        assertEquals(DownloadNetwork.ANY, merged.form!!.downloadNetwork)
        assertTrue(merged.form.autoAlternatives)
        assertTrue(merged.conflicts.isEmpty())
        assertEquals(setOf(PreferenceField.AUTO_ALTERNATIVES), merged.dirty)
        mine.events.stop()
        coroutineContext.cancelChildren()
    }

    @Test
    fun a_stale_save_meets_a_conflict_and_writes_only_after_keep_mine() = test {
        val db = newDb()
        val mine = runtime(db)
        val other = runtime(db)
        val flow = flow(mine)
        flow.refresh()
        flow.awaitState { it.base != null }
        flow.edit { it.copy(maxActiveDownloads = 2) }
        val write = other.call(
            SettingsFlow.UPDATE,
            JSONObject().put("changes", JSONObject().put("max_active_downloads", 3).put("subs", "pl")).put("expected_revision", 0),
        )
        assertTrue(write is BridgeResult.Ok)

        flow.save()
        val clash = flow.awaitState { !it.saving }
        assertEquals(listOf(PreferenceField.MAX_ACTIVE_DOWNLOADS), clash.conflicts.map { it.field })
        assertEquals(1L, clash.base!!.revision)
        assertEquals("pl", clash.form!!.subsText)
        assertEquals(1L, ((other.call(SettingsFlow.GET) as BridgeResult.Ok).data.getLong("revision")))

        flow.resolve(keepMine = true)
        val kept = flow.awaitState { !it.saving && it.result != null }
        assertEquals(SettingsFlow.SaveResult.Saved, kept.result)
        val stored = (other.call(SettingsFlow.GET) as BridgeResult.Ok).data
        assertEquals(2L, stored.getLong("revision"))
        assertEquals(2, stored.getInt("max_active_downloads"))
        assertEquals("pl", stored.getString("subs"))
    }

    @Test
    fun the_engine_still_refuses_values_the_ui_would_not_send() = test {
        val bridge = runtime(newDb())
        val reply = bridge.call(SettingsFlow.UPDATE, JSONObject().put("changes", JSONObject().put("audio", "none")).put("expected_revision", 0))
        reply as BridgeResult.Failure
        assertEquals("invalid_input", reply.kind)
        assertEquals("invalid_preferences", reply.params.optString("problem"))
        assertEquals(0L, (bridge.call(SettingsFlow.GET) as BridgeResult.Ok).data.getLong("revision"))
    }
}
