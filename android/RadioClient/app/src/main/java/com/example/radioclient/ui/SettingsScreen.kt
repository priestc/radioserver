package com.example.radioclient.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import android.os.Handler
import android.os.Looper
import com.example.radioclient.RadioClientApp
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

@Composable
fun SettingsScreen(app: RadioClientApp) {
    val settings = app.settingsManager
    val network = app.networkMonitor

    val localURL by settings.localURL.collectAsState()
    val remoteURL by settings.remoteURL.collectAsState()
    val apiKey by settings.apiKey.collectAsState()
    val bufferCacheMB by settings.bufferCacheMB.collectAsState()
    val isOnWifi by network.isOnWifi.collectAsState()

    var testResult by remember { mutableStateOf<String?>(null) }
    var isTesting by remember { mutableStateOf(false) }
    var showScanner by remember { mutableStateOf(false) }

    if (showScanner) {
        QRScannerScreen(
            onScanned = { value ->
                settings.setApiKey(value)
                showScanner = false
            },
            onDismiss = { showScanner = false },
        )
        return
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
    ) {
        // Server section
        Text("Server", style = MaterialTheme.typography.titleMedium)
        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = localURL,
            onValueChange = { settings.setLocalURL(it) },
            label = { Text("Local IP") },
            placeholder = { Text("192.168.1.50") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = remoteURL,
            onValueChange = { settings.setRemoteURL(it) },
            label = { Text("Remote IP") },
            placeholder = { Text("100.64.0.1") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(modifier = Modifier.height(4.dp))

        Text(
            text = if (isOnWifi) "Using Local" else "Using Remote",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(modifier = Modifier.height(8.dp))

        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth(),
        ) {
            OutlinedTextField(
                value = apiKey,
                onValueChange = { settings.setApiKey(it) },
                label = { Text("API Key") },
                singleLine = true,
                textStyle = MaterialTheme.typography.bodyMedium.copy(
                    fontFamily = FontFamily.Monospace,
                ),
                modifier = Modifier.weight(1f),
            )
            Spacer(modifier = Modifier.width(8.dp))
            IconButton(onClick = { showScanner = true }) {
                Icon(
                    imageVector = Icons.Default.QrCodeScanner,
                    contentDescription = "Scan QR Code",
                )
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        // Test connection
        Text("Connection", style = MaterialTheme.typography.titleMedium)
        Spacer(modifier = Modifier.height(8.dp))

        Button(
            onClick = {
                isTesting = true
                testResult = null
                val baseURL = app.apiService.buildBaseURL()
                val apiKey = app.settingsManager.apiKey.value
                val mainHandler = Handler(Looper.getMainLooper())
                Thread {
                    try {
                        val client = OkHttpClient.Builder()
                            .connectTimeout(15, TimeUnit.SECONDS)
                            .readTimeout(15, TimeUnit.SECONDS)
                            .build()
                        val body = """{"played":[],"buffer_cache_mb":0}"""
                            .toRequestBody("application/json".toMediaType())
                        val request = Request.Builder()
                            .url("${baseURL}library/api/client_sync/")
                            .addHeader("Authorization", "Bearer $apiKey")
                            .post(body)
                            .build()
                        val response = client.newCall(request).execute()
                        val responseBody = response.body?.string() ?: ""
                        if (response.isSuccessful) {
                            val json = Json { ignoreUnknownKeys = true }
                            val syncResponse = json.decodeFromString(
                                com.example.radioclient.model.SyncResponse.serializer(),
                                responseBody,
                            )
                            mainHandler.post {
                                testResult = "Connected! ${syncResponse.download.size} songs available"
                                isTesting = false
                            }
                        } else {
                            mainHandler.post {
                                testResult = "Failed: Server error ${response.code}"
                                isTesting = false
                            }
                        }
                    } catch (e: Exception) {
                        mainHandler.post {
                            testResult = "Failed: ${e.message}"
                            isTesting = false
                        }
                    }
                }.start()
            },
            enabled = !isTesting,
        ) {
            Text(if (isTesting) "Testing..." else "Test Connection")
        }

        testResult?.let { result ->
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = result,
                style = MaterialTheme.typography.bodySmall,
                color = if (result.startsWith("Connected"))
                    MaterialTheme.colorScheme.primary
                else
                    MaterialTheme.colorScheme.error,
            )
        }

        Spacer(modifier = Modifier.height(16.dp))

        // Cache section
        Text("Cache", style = MaterialTheme.typography.titleMedium)
        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = bufferCacheMB.toString(),
            onValueChange = { value ->
                value.toIntOrNull()?.let { settings.setBufferCacheMB(it) }
            },
            label = { Text("Buffer Size (MB)") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(modifier = Modifier.height(8.dp))

        val songCacheMB = remember { app.cacheManager.totalSongCacheSizeMB() }
        val artworkCacheMB = remember { app.cacheManager.totalArtworkCacheSizeMB() }
        Text(
            text = "Audio Cache: %.1f MB".format(songCacheMB),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = "Artwork Cache: %.1f MB".format(artworkCacheMB),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(modifier = Modifier.height(8.dp))

        Button(
            onClick = { app.cacheManager.clearCache() },
            colors = ButtonDefaults.buttonColors(
                containerColor = MaterialTheme.colorScheme.error,
            ),
        ) {
            Text("Clear Cache")
        }
    }
}
