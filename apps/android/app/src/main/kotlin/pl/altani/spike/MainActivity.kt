package pl.altani.spike

import android.content.res.Configuration
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.util.UnstableApi
import kotlinx.coroutines.launch
import pl.altani.spike.ui.CatalogScreen
import pl.altani.spike.ui.CatalogViewModel
import pl.altani.spike.ui.FullscreenPlayer
import pl.altani.spike.ui.OrientationLock
import pl.altani.spike.ui.PlaybackViewModel
import pl.altani.spike.ui.SettingsScreen
import pl.altani.spike.ui.SettingsViewModel
import pl.altani.spike.ui.TechScreen
import pl.altani.spike.ui.TechViewModel

@UnstableApi
class MainActivity : ComponentActivity() {
    private val model: TechViewModel by viewModels()
    private val catalog: CatalogViewModel by viewModels()
    private val settings: SettingsViewModel by viewModels()
    val playback: PlaybackViewModel by viewModels()
    private lateinit var orientation: OrientationLock

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        orientation = OrientationLock(this)
        playback.onViewAttached(recreated = savedInstanceState != null)
        // collected for the Activity's whole life, so a save made while the player is fullscreen still refreshes the history
        lifecycleScope.launch { playback.catalogSaved.collect(catalog.flow::onWatchSaved) }
        setContent {
            MaterialTheme {
                Surface { Content() }
            }
        }
    }

    @Composable
    private fun Content() {
        var tab by rememberSaveable { mutableIntStateOf(CATALOG_TAB) }
        val mode by playback.mode.collectAsStateWithLifecycle()
        // the controlled flow is driven from the Tech tab, so its consent button must be on screen
        LaunchedEffect(mode) { if (mode == PlaybackViewModel.Mode.CONTROLLED) tab = TECH_TAB }
        val player by playback.catalogController.collectAsStateWithLifecycle()
        val landscape = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE
        val candidate = player?.takeIf { landscape && tab == CATALOG_TAB && mode == PlaybackViewModel.Mode.CATALOG }
        val state = candidate?.state?.collectAsStateWithLifecycle()?.value
        // an error or the consent button needs the regular layout to be seen
        val fullscreen = state != null && state.error == null && !state.consentRequired
        SystemBars(hidden = fullscreen)
        if (fullscreen) {
            FullscreenPlayer(candidate) { orientation.force(landscape = false) }
            return
        }
        Column(Modifier.fillMaxSize().safeDrawingPadding()) {
            PrimaryTabRow(selectedTabIndex = tab) {
                Tab(selected = tab == CATALOG_TAB, onClick = { tab = CATALOG_TAB }, text = { Text("Catalog") })
                Tab(selected = tab == SETTINGS_TAB, onClick = { tab = SETTINGS_TAB }, text = { Text("Settings") })
                Tab(selected = tab == TECH_TAB, onClick = { tab = TECH_TAB }, text = { Text("Tech") })
            }
            when (tab) {
                CATALOG_TAB -> CatalogScreen(catalog, playback, orientation::force)
                SETTINGS_TAB -> SettingsScreen(settings)
                else -> TechScreen(model, playback)
            }
        }
    }

    @Composable
    private fun SystemBars(hidden: Boolean) {
        DisposableEffect(hidden) {
            val bars = WindowCompat.getInsetsController(window, window.decorView)
            if (hidden) {
                bars.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                bars.hide(WindowInsetsCompat.Type.systemBars())
            } else {
                bars.show(WindowInsetsCompat.Type.systemBars())
            }
            onDispose {}
        }
    }

    override fun onResume() {
        super.onResume()
        orientation.enable()
    }

    override fun onPause() {
        orientation.disable()
        super.onPause()
    }

    override fun onStop() {
        super.onStop()
        // rotation keeps playing; Home, another app and the screen lock pause and save the position
        if (!isChangingConfigurations) playback.onBackground()
    }

    private companion object {
        const val CATALOG_TAB = 0
        const val SETTINGS_TAB = 1
        const val TECH_TAB = 2
    }
}
