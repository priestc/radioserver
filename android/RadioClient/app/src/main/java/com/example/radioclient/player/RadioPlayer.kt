package com.example.radioclient.player

import android.graphics.Bitmap
import android.net.Uri
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import com.example.radioclient.model.PlayedSong
import com.example.radioclient.model.SongItem
import com.example.radioclient.model.SyncRequest
import com.example.radioclient.model.SyncRequestNowPlaying
import com.example.radioclient.model.SyncRequestPlayed
import com.example.radioclient.service.ApiService
import com.example.radioclient.service.CacheManager
import com.example.radioclient.service.NetworkMonitor
import com.example.radioclient.service.SettingsManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import kotlin.math.min
import kotlin.math.pow

class RadioPlayer(
    private val exoPlayer: ExoPlayer,
    private val apiService: ApiService,
    private val cacheManager: CacheManager,
    private val networkMonitor: NetworkMonitor,
    private val settingsManager: SettingsManager,
) {
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private val _currentSong = MutableStateFlow<SongItem?>(null)
    val currentSong: StateFlow<SongItem?> = _currentSong

    private val _queue = MutableStateFlow<List<SongItem>>(emptyList())
    val queue: StateFlow<List<SongItem>> = _queue

    private val _playHistory = MutableStateFlow<List<PlayedSong>>(emptyList())
    val playHistory: StateFlow<List<PlayedSong>> = _playHistory

    private val _isPlaying = MutableStateFlow(false)
    val isPlaying: StateFlow<Boolean> = _isPlaying

    private val _currentTime = MutableStateFlow(0.0)
    val currentTime: StateFlow<Double> = _currentTime

    private val _duration = MutableStateFlow(0.0)
    val duration: StateFlow<Double> = _duration

    private val _artworkCache = MutableStateFlow<Map<Int, Bitmap>>(emptyMap())
    val artworkCache: StateFlow<Map<Int, Bitmap>> = _artworkCache

    private val pendingPlayed = mutableListOf<PlayedSong>()
    private val artworkFailed = mutableSetOf<Int>()
    private var hasSyncedCurrentSong = false
    private var currentSongStartedAt: Instant? = null
    private var syncBackoffSeconds = 2.0
    private var syncRetryJob: Job? = null
    private var timeObserverJob: Job? = null

    private val isoFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'")
        .withZone(ZoneOffset.UTC)

    init {
        exoPlayer.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) {
                    onSongFinished()
                }
            }

            override fun onIsPlayingChanged(playing: Boolean) {
                _isPlaying.value = playing
            }
        })
    }

    fun startSyncTimer() {
        scope.launch { performSyncWithRetry() }
    }

    fun togglePlayPause() {
        if (exoPlayer.isPlaying) {
            exoPlayer.pause()
        } else {
            if (_currentSong.value == null && _queue.value.isEmpty()) {
                triggerSync()
                return
            }
            exoPlayer.play()
        }
    }

    fun skipToNext() {
        val song = _currentSong.value ?: return
        val played = PlayedSong(
            song = song,
            playedAt = Instant.now(),
            skipped = true,
        )
        if (!hasSyncedCurrentSong) {
            pendingPlayed.add(played)
        }
        addToHistory(played)
        cleanupCachedFiles(song)
        playNext()
        triggerSync()
    }

    fun playNext() {
        val q = _queue.value
        if (q.isEmpty()) {
            stopPlayback()
            return
        }
        val next = q.first()
        _queue.value = q.drop(1)
        playSong(next)
    }

    fun playSong(song: SongItem) {
        stopTimeObserver()
        exoPlayer.stop()

        _currentSong.value = song
        currentSongStartedAt = Instant.now()
        hasSyncedCurrentSong = false

        // Find cached file: prefer native format, fall back to mp3
        val file = when {
            cacheManager.hasCachedSong(song.id, song.fileExtension) ->
                cacheManager.songFile(song.id, song.fileExtension)
            cacheManager.hasCachedSong(song.id, "mp3") ->
                cacheManager.songFile(song.id, "mp3")
            else -> null
        }

        if (file == null) {
            // No cached file — skip to next
            playNext()
            return
        }

        val mediaItem = MediaItem.Builder()
            .setUri(Uri.fromFile(file))
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(song.title)
                    .setArtist(song.artist)
                    .setAlbumTitle(song.album)
                    .build()
            )
            .build()

        exoPlayer.setMediaItem(mediaItem)
        exoPlayer.prepare()

        // Apply ReplayGain
        applyReplayGain(song)

        exoPlayer.play()

        startTimeObserver()
        loadArtwork(song)
    }

    private fun applyReplayGain(song: SongItem) {
        val gainDB = song.replaygainTrackGain
        if (gainDB != null) {
            val linear = 10.0.pow(gainDB / 20.0)
            exoPlayer.volume = min(linear, 1.0).toFloat()
        } else {
            exoPlayer.volume = 1.0f
        }
    }

    private fun startTimeObserver() {
        timeObserverJob = scope.launch {
            while (isActive) {
                delay(500)
                if (exoPlayer.playbackState == Player.STATE_READY ||
                    exoPlayer.playbackState == Player.STATE_BUFFERING
                ) {
                    val pos = exoPlayer.currentPosition / 1000.0
                    val dur = exoPlayer.duration.let { if (it > 0) it / 1000.0 else 0.0 }
                    _currentTime.value = pos
                    _duration.value = dur

                    // Sync at 50% playback
                    if (!hasSyncedCurrentSong && dur > 0 && pos >= dur / 2) {
                        hasSyncedCurrentSong = true
                        val song = _currentSong.value
                        if (song != null) {
                            val played = PlayedSong(
                                song = song,
                                playedAt = Instant.now(),
                                skipped = false,
                            )
                            pendingPlayed.add(played)
                            triggerSync()
                        }
                    }
                }
            }
        }
    }

    private fun stopTimeObserver() {
        timeObserverJob?.cancel()
        timeObserverJob = null
    }

    private fun onSongFinished() {
        val song = _currentSong.value ?: return

        // If we haven't synced yet (song < 50%), add to pending
        if (!hasSyncedCurrentSong) {
            val played = PlayedSong(
                song = song,
                playedAt = Instant.now(),
                skipped = false,
            )
            pendingPlayed.add(played)
        }

        // Always add to visible history
        addToHistory(PlayedSong(song = song, playedAt = Instant.now(), skipped = false))

        cleanupCachedFiles(song)
        playNext()
        triggerSync()
    }

    private fun cleanupCachedFiles(song: SongItem) {
        cacheManager.removeSong(song.id, song.fileExtension)
        if (song.fileExtension != "mp3") {
            cacheManager.removeSong(song.id, "mp3")
        }
    }

    private fun addToHistory(played: PlayedSong) {
        _playHistory.value = listOf(played) + _playHistory.value
    }

    private fun stopPlayback() {
        stopTimeObserver()
        exoPlayer.stop()
        _currentSong.value = null
        _currentTime.value = 0.0
        _duration.value = 0.0
    }

    fun triggerSync() {
        syncBackoffSeconds = 2.0
        syncRetryJob?.cancel()
        syncRetryJob = scope.launch { performSyncWithRetry() }
    }

    private suspend fun performSyncWithRetry() {
        try {
            while (true) {
                val success = performSync()
                if (success) return

                val delayMs = (syncBackoffSeconds * 1000).toLong()
                syncBackoffSeconds = min(syncBackoffSeconds * 2, 60.0)
                delay(delayMs)
            }
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private suspend fun performSync(): Boolean {
        val serverURL = apiService.activeServerURL
        val apiKey = settingsManager.apiKey.value
        if (serverURL.isBlank() || apiKey.isBlank()) return false

        try {
            val playedItems = pendingPlayed.map { played ->
                SyncRequestPlayed(
                    id = played.song.id,
                    playedAt = isoFormatter.format(played.playedAt),
                    skipped = played.skipped,
                )
            }

            val nowPlaying = _currentSong.value?.let { song ->
                currentSongStartedAt?.let { startedAt ->
                    SyncRequestNowPlaying(
                        id = song.id,
                        startedAt = isoFormatter.format(startedAt),
                    )
                }
            }

            val request = SyncRequest(
                played = playedItems,
                bufferCacheMb = settingsManager.bufferCacheMB.value,
                nowPlaying = nowPlaying,
            )

            val result = apiService.sync(request)
            if (result.isFailure) return false

            val response = result.getOrNull() ?: return false

        // Clear sent pending items
        val sentCount = playedItems.size
        if (sentCount > 0 && pendingPlayed.size >= sentCount) {
            repeat(sentCount) { pendingPlayed.removeFirstOrNull() }
        }

        // Add new songs to queue (skip duplicates)
        val existingIds = _queue.value.map { it.id }.toSet() +
            setOfNotNull(_currentSong.value?.id)
        val newSongs = response.download.filter { it.id !in existingIds }

        if (newSongs.isNotEmpty()) {
            _queue.value = _queue.value + newSongs

            // Download songs
            scope.launch { downloadNewSongs(newSongs) }
        }

        // Auto-start if idle and queue has songs
        if (_currentSong.value == null && _queue.value.isNotEmpty()) {
            playNext()
        }

            syncBackoffSeconds = 2.0
            return true
        } catch (e: Exception) {
            e.printStackTrace()
            return false
        }
    }

    private suspend fun downloadNewSongs(songs: List<SongItem>) {
        val onCellular = networkMonitor.isCellular.value

        if (onCellular) {
            // Cellular: download max 2 low-bitrate songs
            var cachedCount = songs.count { song ->
                cacheManager.hasCachedSong(song.id, song.fileExtension) ||
                    cacheManager.hasCachedSong(song.id, "mp3")
            }
            for (song in songs) {
                if (cachedCount >= 2) break
                if (cacheManager.hasCachedSong(song.id, song.fileExtension) ||
                    cacheManager.hasCachedSong(song.id, "mp3")
                ) continue

                withContext(Dispatchers.IO) {
                    apiService.downloadSongLowBitrate(song.id).onSuccess { data ->
                        cacheManager.saveSong(song.id, "mp3", data)
                        cachedCount++
                    }
                    downloadArtworkIfNeeded(song)
                }
            }
        } else {
            // WiFi: download all at full quality
            for (song in songs) {
                if (cacheManager.hasCachedSong(song.id, song.fileExtension) ||
                    cacheManager.hasCachedSong(song.id, "mp3")
                ) continue

                withContext(Dispatchers.IO) {
                    apiService.downloadSong(song.id).onSuccess { data ->
                        cacheManager.saveSong(song.id, song.fileExtension, data)
                    }
                    downloadArtworkIfNeeded(song)
                }
            }
        }
    }

    private suspend fun downloadArtworkIfNeeded(song: SongItem) {
        val albumId = song.albumId ?: return
        if (artworkFailed.contains(albumId)) return
        if (cacheManager.hasCachedArtwork(albumId)) {
            // Load into memory cache if not already there
            if (!_artworkCache.value.containsKey(albumId)) {
                cacheManager.cachedArtwork(albumId)?.let { bitmap ->
                    _artworkCache.value = _artworkCache.value + (albumId to bitmap)
                }
            }
            return
        }

        withContext(Dispatchers.IO) {
            apiService.downloadArtwork(albumId).onSuccess { data ->
                cacheManager.saveArtwork(albumId, data)
                cacheManager.cachedArtwork(albumId)?.let { bitmap ->
                    _artworkCache.value = _artworkCache.value + (albumId to bitmap)
                }
            }.onFailure {
                artworkFailed.add(albumId)
            }
        }
    }

    private fun loadArtwork(song: SongItem) {
        val albumId = song.albumId ?: return
        if (_artworkCache.value.containsKey(albumId)) {
            updateMediaSessionArtwork(song)
            return
        }
        scope.launch {
            downloadArtworkIfNeeded(song)
            updateMediaSessionArtwork(song)
        }
    }

    private fun updateMediaSessionArtwork(song: SongItem) {
        val albumId = song.albumId ?: return
        val bitmap = _artworkCache.value[albumId] ?: return
        val artworkUri = Uri.parse(apiService.coverArtURL(albumId) ?: return)

        val metadata = MediaMetadata.Builder()
            .setTitle(song.title)
            .setArtist(song.artist)
            .setAlbumTitle(song.album)
            .setArtworkUri(artworkUri)
            .build()

        val currentItem = exoPlayer.currentMediaItem ?: return
        val updated = currentItem.buildUpon()
            .setMediaMetadata(metadata)
            .build()
        exoPlayer.replaceMediaItem(0, updated)
    }

    fun destroy() {
        stopTimeObserver()
        syncRetryJob?.cancel()
        scope.cancel()
    }
}
