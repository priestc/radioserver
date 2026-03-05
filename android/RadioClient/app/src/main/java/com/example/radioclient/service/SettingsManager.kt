package com.example.radioclient.service

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

class SettingsManager(context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences("radio_client_prefs", Context.MODE_PRIVATE)

    private val _localURL = MutableStateFlow(prefs.getString("localURL", "") ?: "")
    val localURL: StateFlow<String> = _localURL

    private val _remoteURL = MutableStateFlow(prefs.getString("remoteURL", "") ?: "")
    val remoteURL: StateFlow<String> = _remoteURL

    private val _apiKey = MutableStateFlow(prefs.getString("apiKey", "") ?: "")
    val apiKey: StateFlow<String> = _apiKey

    private val _bufferCacheMB = MutableStateFlow(prefs.getInt("bufferCacheMB", 100))
    val bufferCacheMB: StateFlow<Int> = _bufferCacheMB

    fun setLocalURL(value: String) {
        _localURL.value = value
        prefs.edit().putString("localURL", value).apply()
    }

    fun setRemoteURL(value: String) {
        _remoteURL.value = value
        prefs.edit().putString("remoteURL", value).apply()
    }

    fun setApiKey(value: String) {
        _apiKey.value = value
        prefs.edit().putString("apiKey", value).apply()
    }

    fun setBufferCacheMB(value: Int) {
        _bufferCacheMB.value = value
        prefs.edit().putInt("bufferCacheMB", value).apply()
    }
}
