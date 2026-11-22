package pl.altani.spike.offline

import android.content.Context
import android.os.Build
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.io.File
import org.json.JSONObject

/** The app's file store: one library under `filesDir`, beside the database, kept out of backups. */
object AppStorage {
    /** Excluded in `data_extraction_rules.xml` and `backup_rules.xml`; a restored database marks its items missing. */
    const val LIBRARY_DIR = "library"

    fun libraryRoot(context: Context): File = File(context.filesDir, LIBRARY_DIR)

    /** `create_engine`'s `storage` argument; the handle lives as long as [bridge], so it is never released. */
    fun register(
        bridge: PyObject,
        root: File,
        authority: String? = null,
        name: String? = null,
        apiLevel: Int = Build.VERSION.SDK_INT,
    ): JSONObject {
        val module = Python.getInstance().getModule("android_storage")
        return JSONObject(module.callAttr("register", bridge, root.absolutePath, authority, name, apiLevel).toString())
    }
}
