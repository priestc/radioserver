package com.example.radioclient.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.time.Instant
import java.util.UUID

@Serializable
data class SongItem(
    val id: Int,
    val title: String,
    val artist: String,
    val album: String? = null,
    @SerialName("album_id") val albumId: Int? = null,
    val year: Int? = null,
    val duration: Double? = null,
    @SerialName("file_format") val fileFormat: String? = null,
    @SerialName("replaygain_track_gain") val replaygainTrackGain: Double? = null,
) {
    val fileExtension: String
        get() = fileFormat?.lowercase() ?: "mp3"
}

data class PlayedSong(
    val id: String = UUID.randomUUID().toString(),
    val song: SongItem,
    val playedAt: Instant,
    val skipped: Boolean,
)

@Serializable
data class SyncResponse(
    val download: List<SongItem>,
)

@Serializable
data class SyncRequestPlayed(
    val id: Int,
    @SerialName("played_at") val playedAt: String,
    val skipped: Boolean,
)

@Serializable
data class SyncRequestNowPlaying(
    val id: Int,
    @SerialName("started_at") val startedAt: String,
)

@Serializable
data class SyncRequest(
    val played: List<SyncRequestPlayed>,
    @SerialName("buffer_cache_mb") val bufferCacheMb: Int,
    @SerialName("now_playing") val nowPlaying: SyncRequestNowPlaying? = null,
)
