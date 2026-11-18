package pl.altani.spike.bridge

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class NetworkSyncTest {
    private val sent = mutableListOf<String>()
    private var requested = true
    private var reply: BridgeResult = BridgeResult.Ok(JSONObject())

    private fun TestScope.sync(initial: String = "wifi") =
        NetworkSync(initial, backgroundScope, { requested }, { kind -> sent += kind; reply })

    @Test
    fun `the initial kind and repeats of the known one are never sent`() = runTest {
        val sync = sync()
        runCurrent()
        sync.update("wifi")
        runCurrent()
        assertEquals(emptyList<String>(), sent)
    }

    @Test
    fun `a change reaches a requested engine once`() = runTest {
        val sync = sync()
        runCurrent()
        sync.update("cellular")
        runCurrent()
        sync.update("cellular")
        runCurrent()
        assertEquals(listOf("cellular"), sent)
        assertEquals("cellular", sync.kind.value)
    }

    @Test
    fun `without an engine the change only updates its creation arguments`() = runTest {
        requested = false
        val sync = sync()
        runCurrent()
        sync.update("offline")
        runCurrent()
        assertEquals(emptyList<String>(), sent)
        assertEquals("offline", sync.kind.value)

        requested = true
        sync.update("wifi")
        runCurrent()
        assertEquals(listOf("wifi"), sent)
    }

    @Test
    fun `a burst is sent as its last kind`() = runTest {
        val sync = sync()
        sync.update("offline")
        sync.update("cellular")
        sync.update("offline")
        runCurrent()
        assertEquals(listOf("offline"), sent)
    }

    @Test
    fun `a refused change is sent again when the kind comes back`() = runTest {
        val sync = sync()
        runCurrent()
        reply = BridgeResult.Failure("engine_closed", "closed", false, JSONObject())
        sync.update("cellular")
        runCurrent()
        reply = BridgeResult.Ok(JSONObject())
        sync.update("offline")
        runCurrent()
        sync.update("cellular")
        runCurrent()
        assertEquals(listOf("cellular", "offline", "cellular"), sent)
    }
}
