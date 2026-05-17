package com.safedrop.android

import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.FileProvider
import com.safedrop.android.data.SafeDropService
import com.safedrop.android.photo.PhotoRequest
import com.safedrop.android.photo.PhotoResult
import com.safedrop.android.ui.HomeScreen
import java.io.File

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val service = (application as SafeDropApplication).service
        setContent {
            val context = LocalContext.current
            val scheme = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                dynamicLightColorScheme(context)
            } else {
                lightColorScheme()
            }
            MaterialTheme(colorScheme = scheme) {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    CameraHost(service) {
                        HomeScreen(service)
                    }
                }
            }
        }
    }
}

/**
 * Sits above [HomeScreen]; the Activity-bound camera launcher lives here so a
 * `take_photo` request emitted by the background tool registry can hop onto
 * the UI thread and trigger the system camera Intent.
 */
@Composable
private fun CameraHost(service: SafeDropService, content: @Composable () -> Unit) {
    val context = LocalContext.current
    var pendingFile by remember { mutableStateOf<File?>(null) }
    var pendingRequest by remember { mutableStateOf<PhotoRequest?>(null) }

    val launcher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.TakePicture()
    ) { success: Boolean ->
        val file = pendingFile
        val req = pendingRequest
        if (req != null) {
            if (success && file != null && file.exists() && file.length() > 0) {
                val bytes = runCatching { file.readBytes() }.getOrNull()
                if (bytes != null) {
                    req.deliver(PhotoResult.Success(bytes, "image/jpeg"))
                } else {
                    req.deliver(PhotoResult.Cancelled("could not read photo file"))
                }
            } else {
                req.deliver(PhotoResult.Cancelled("user cancelled or empty result"))
            }
        }
        try { file?.delete() } catch (_: Exception) {}
        pendingFile = null
        pendingRequest = null
    }

    LaunchedEffect(Unit) {
        service.photoCapturer.requests.collect { req ->
            val cacheDir = context.cacheDir
            val file = File.createTempFile("safedrop_photo_", ".jpg", cacheDir)
            val uri: Uri = FileProvider.getUriForFile(
                context, "${context.packageName}.fileprovider", file
            )
            pendingFile = file
            pendingRequest = req
            try {
                launcher.launch(uri)
            } catch (e: Exception) {
                // No camera app installed, or launch failed — deliver an error
                // so the suspended tool handler can return promptly.
                req.deliver(PhotoResult.Cancelled("camera launch failed: ${e.message}"))
                pendingFile = null
                pendingRequest = null
            }
        }
    }

    content()
}
