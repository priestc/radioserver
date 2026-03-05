package com.example.radioclient.service

import com.example.radioclient.model.SyncRequest
import com.example.radioclient.model.SyncResponse
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.net.URL
import java.util.concurrent.TimeUnit

class ApiService(
    private val settingsManager: SettingsManager,
    private val networkMonitor: NetworkMonitor,
) {
    private val json = Json { ignoreUnknownKeys = true }

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    val activeServerURL: String
        get() = if (networkMonitor.isOnWifi.value) {
            settingsManager.localURL.value
        } else {
            settingsManager.remoteURL.value
        }

    fun buildBaseURL(serverURL: String = activeServerURL): String? {
        if (serverURL.isBlank()) return null
        var urlStr = serverURL.trim()
        if (!urlStr.startsWith("http://") && !urlStr.startsWith("https://")) {
            urlStr = "http://$urlStr"
        }
        val parsed = try { URL(urlStr) } catch (_: Exception) { return null }
        val withPort = if (parsed.port == -1) {
            URL(parsed.protocol, parsed.host, 9437, parsed.file)
        } else {
            parsed
        }
        val str = withPort.toString()
        return if (str.endsWith("/")) str else "$str/"
    }

    fun coverArtURL(albumId: Int): String? {
        val base = buildBaseURL() ?: return null
        return "${base}library/cover/$albumId/"
    }

    suspend fun sync(request: SyncRequest): Result<SyncResponse> = runCatching {
        val base = buildBaseURL() ?: throw IllegalStateException("No server URL configured")
        val apiKey = settingsManager.apiKey.value
        val body = json.encodeToString(SyncRequest.serializer(), request)
            .toRequestBody("application/json".toMediaType())

        val httpRequest = Request.Builder()
            .url("${base}library/api/client_sync/")
            .addHeader("Authorization", "Bearer $apiKey")
            .post(body)
            .build()

        val response = client.newCall(httpRequest).execute()
        if (!response.isSuccessful) throw RuntimeException("Server error: ${response.code}")
        val responseBody = response.body?.string() ?: throw RuntimeException("Empty response")
        json.decodeFromString(SyncResponse.serializer(), responseBody)
    }

    suspend fun downloadSong(playlistItemId: Int): Result<ByteArray> = runCatching {
        val base = buildBaseURL() ?: throw IllegalStateException("No server URL configured")
        val apiKey = settingsManager.apiKey.value

        val request = Request.Builder()
            .url("${base}library/api/download_song/$playlistItemId/")
            .addHeader("Authorization", "Bearer $apiKey")
            .get()
            .build()

        val response = client.newCall(request).execute()
        if (!response.isSuccessful) throw RuntimeException("Download failed: ${response.code}")
        response.body?.bytes() ?: throw RuntimeException("Empty body")
    }

    suspend fun downloadSongLowBitrate(playlistItemId: Int): Result<ByteArray> = runCatching {
        val base = buildBaseURL() ?: throw IllegalStateException("No server URL configured")
        val apiKey = settingsManager.apiKey.value

        val request = Request.Builder()
            .url("${base}library/api/download_song_lowbitrate/$playlistItemId/")
            .addHeader("Authorization", "Bearer $apiKey")
            .get()
            .build()

        val response = client.newCall(request).execute()
        if (!response.isSuccessful) throw RuntimeException("Download failed: ${response.code}")
        response.body?.bytes() ?: throw RuntimeException("Empty body")
    }

    suspend fun downloadArtwork(albumId: Int): Result<ByteArray> = runCatching {
        val base = buildBaseURL() ?: throw IllegalStateException("No server URL configured")

        val request = Request.Builder()
            .url("${base}library/cover/$albumId/")
            .get()
            .build()

        val response = client.newCall(request).execute()
        if (!response.isSuccessful) throw RuntimeException("Artwork not found: ${response.code}")
        response.body?.bytes() ?: throw RuntimeException("Empty body")
    }
}
