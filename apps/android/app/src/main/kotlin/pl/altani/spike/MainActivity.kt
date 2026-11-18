package pl.altani.spike

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.media3.common.util.UnstableApi
import pl.altani.spike.ui.PlaybackViewModel
import pl.altani.spike.ui.TechScreen
import pl.altani.spike.ui.TechViewModel

@UnstableApi
class MainActivity : ComponentActivity() {
    private val model: TechViewModel by viewModels()
    val playback: PlaybackViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        playback.controller?.onViewAttached(recreated = savedInstanceState != null)
        setContent {
            MaterialTheme {
                TechScreen(model, playback)
            }
        }
    }

    override fun onStop() {
        super.onStop()
        // rotation keeps playing; Home, another app and the screen lock pause and save the position
        if (!isChangingConfigurations) playback.controller?.onBackground()
    }
}
