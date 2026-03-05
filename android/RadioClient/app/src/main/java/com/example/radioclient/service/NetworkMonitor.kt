package com.example.radioclient.service

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

class NetworkMonitor(context: Context) {
    private val connectivityManager =
        context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    private val _isOnWifi = MutableStateFlow(false)
    val isOnWifi: StateFlow<Boolean> = _isOnWifi

    private val _isCellular = MutableStateFlow(false)
    val isCellular: StateFlow<Boolean> = _isCellular

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onCapabilitiesChanged(
            network: Network,
            capabilities: NetworkCapabilities,
        ) {
            _isOnWifi.value = capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
            _isCellular.value = capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)
        }

        override fun onLost(network: Network) {
            _isOnWifi.value = false
            _isCellular.value = false
        }
    }

    init {
        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        connectivityManager.registerNetworkCallback(request, networkCallback)

        // Check current state
        val activeNetwork = connectivityManager.activeNetwork
        val caps = activeNetwork?.let { connectivityManager.getNetworkCapabilities(it) }
        _isOnWifi.value = caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        _isCellular.value = caps?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) == true
    }

    fun stop() {
        connectivityManager.unregisterNetworkCallback(networkCallback)
    }
}
