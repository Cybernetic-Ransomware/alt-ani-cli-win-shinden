package pl.altani.spike

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import pl.altani.spike.ui.CatalogScreen
import pl.altani.spike.ui.CatalogViewModel
import pl.altani.spike.ui.PlaybackViewModel
import pl.altani.spike.ui.TechScreen
import pl.altani.spike.ui.TechViewModel

@UnstableApi
class MainActivity : ComponentActivity() {
    private val model: TechViewModel by viewModels()
    private val catalog: CatalogViewModel by viewModels()
    val playback: PlaybackViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        playback.onViewAttached(recreated = savedInstanceState != null)
        setContent {
            MaterialTheme {
                Surface {
                    var tab by rememberSaveable { mutableIntStateOf(0) }
                    val mode by playback.mode.collectAsStateWithLifecycle()
                    // the controlled flow is driven from the Tech tab, so its consent button must be on screen
                    LaunchedEffect(mode) { if (mode == PlaybackViewModel.Mode.CONTROLLED) tab = 1 }
                    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
                        PrimaryTabRow(selectedTabIndex = tab) {
                            Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Catalog") })
                            Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Tech") })
                        }
                        if (tab == 0) CatalogScreen(catalog, playback) else TechScreen(model, playback)
                    }
                }
            }
        }
    }

    override fun onStop() {
        super.onStop()
        // rotation keeps playing; Home, another app and the screen lock pause and save the position
        if (!isChangingConfigurations) playback.onBackground()
    }
}
