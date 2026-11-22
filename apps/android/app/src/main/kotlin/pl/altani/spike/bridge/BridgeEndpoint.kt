package pl.altani.spike.bridge

import com.chaquo.python.PyObject

/** The Python `Bridge` as plain JSON strings; every method blocks, so none may run on the main thread. */
interface BridgeEndpoint {
    fun createEngine(payloadJson: String): String

    fun call(method: String, payloadJson: String): String

    fun cancel(opId: String): Boolean

    fun shutdown(timeoutSec: Double): String
}

class ChaquopyEndpoint(private val bridge: PyObject) : BridgeEndpoint {
    override fun createEngine(payloadJson: String): String = bridge.callAttr("create_engine", payloadJson).toString()

    override fun call(method: String, payloadJson: String): String = bridge.callAttr("call", method, payloadJson).toString()

    override fun cancel(opId: String): Boolean = bridge.callAttr("cancel", opId).toBoolean()

    override fun shutdown(timeoutSec: Double): String = bridge.callAttr("shutdown", timeoutSec).toString()
}
