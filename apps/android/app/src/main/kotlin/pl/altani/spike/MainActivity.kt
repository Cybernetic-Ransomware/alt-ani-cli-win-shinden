package pl.altani.spike

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Feasibility screen only: runs the Python-side checks off the main thread and shows their JSON report. */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                var report by remember { mutableStateOf("running…") }
                LaunchedEffect(Unit) {
                    report = withContext(Dispatchers.IO) {
                        Python.getInstance()
                            .getModule("feasibility")
                            .callAttr("run", filesDir.absolutePath, cacheDir.absolutePath)
                            .toString()
                    }
                }
                Text(report, Modifier.padding(16.dp).verticalScroll(rememberScrollState()))
            }
        }
    }
}
