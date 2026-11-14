package pl.altani.spike

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import pl.altani.spike.ui.TechScreen
import pl.altani.spike.ui.TechViewModel

class MainActivity : ComponentActivity() {
    private val model: TechViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                TechScreen(model)
            }
        }
    }
}
