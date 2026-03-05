package com.example.radioclient

import android.app.Application
import androidx.media3.exoplayer.ExoPlayer
import com.example.radioclient.player.RadioPlayer
import com.example.radioclient.service.ApiService
import com.example.radioclient.service.CacheManager
import com.example.radioclient.service.NetworkMonitor
import com.example.radioclient.service.SettingsManager

class RadioClientApp : Application() {
    lateinit var settingsManager: SettingsManager
    lateinit var networkMonitor: NetworkMonitor
    lateinit var cacheManager: CacheManager
    lateinit var apiService: ApiService
    var radioPlayer: RadioPlayer? = null

    override fun onCreate() {
        super.onCreate()
        settingsManager = SettingsManager(this)
        networkMonitor = NetworkMonitor(this)
        cacheManager = CacheManager(this)
        apiService = ApiService(settingsManager, networkMonitor)
    }

    fun initializePlayer(exoPlayer: ExoPlayer) {
        if (radioPlayer != null) return
        radioPlayer = RadioPlayer(
            exoPlayer = exoPlayer,
            apiService = apiService,
            cacheManager = cacheManager,
            networkMonitor = networkMonitor,
            settingsManager = settingsManager,
        )
        radioPlayer?.startSyncTimer()
    }
}
