package com.example.radioclient.service

import android.content.Intent
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.LibraryResult
import androidx.media3.session.MediaLibraryService
import androidx.media3.session.MediaLibraryService.LibraryParams
import androidx.media3.session.MediaLibraryService.MediaLibrarySession
import androidx.media3.session.MediaSession
import com.example.radioclient.RadioClientApp
import com.google.common.collect.ImmutableList
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

class AudioPlayerService : MediaLibraryService() {
    private var mediaLibrarySession: MediaLibrarySession? = null

    private val callback = object : MediaLibrarySession.Callback {
        override fun onGetLibraryRoot(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            params: LibraryParams?,
        ): ListenableFuture<LibraryResult<MediaItem>> {
            val rootItem = MediaItem.Builder()
                .setMediaId("root")
                .setMediaMetadata(
                    MediaMetadata.Builder()
                        .setIsBrowsable(true)
                        .setIsPlayable(false)
                        .setTitle("RadioClient")
                        .build()
                )
                .build()
            return Futures.immediateFuture(LibraryResult.ofItem(rootItem, params))
        }

        override fun onGetChildren(
            session: MediaLibrarySession,
            browser: MediaSession.ControllerInfo,
            parentId: String,
            page: Int,
            pageSize: Int,
            params: LibraryParams?,
        ): ListenableFuture<LibraryResult<ImmutableList<MediaItem>>> {
            val app = application as RadioClientApp
            val player = app.radioPlayer

            if (parentId != "root") {
                return Futures.immediateFuture(
                    LibraryResult.ofItemList(ImmutableList.of(), params)
                )
            }

            val items = mutableListOf<MediaItem>()

            // "All Music" — clears any active channel
            val selectedId = player?.selectedChannelId
            val allMusicTitle = if (selectedId == null) "✓ All Music" else "All Music"
            items.add(
                MediaItem.Builder()
                    .setMediaId("channel_all")
                    .setMediaMetadata(
                        MediaMetadata.Builder()
                            .setIsBrowsable(false)
                            .setIsPlayable(true)
                            .setTitle(allMusicTitle)
                            .build()
                    )
                    .build()
            )

            // Each configured channel
            val channels = player?.channels?.value ?: emptyList()
            for (channel in channels) {
                val title = if (selectedId == channel.id) "✓ ${channel.name}" else channel.name
                items.add(
                    MediaItem.Builder()
                        .setMediaId("channel_${channel.id}")
                        .setMediaMetadata(
                            MediaMetadata.Builder()
                                .setIsBrowsable(false)
                                .setIsPlayable(true)
                                .setTitle(title)
                                .build()
                        )
                        .build()
                )
            }

            return Futures.immediateFuture(
                LibraryResult.ofItemList(ImmutableList.copyOf(items), params)
            )
        }

        override fun onSetMediaItems(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo,
            mediaItems: MutableList<MediaItem>,
            startIndex: Int,
            startPositionMs: Long,
        ): ListenableFuture<MediaSession.MediaItemsWithStartPosition> {
            val mediaId = mediaItems.firstOrNull()?.mediaId ?: ""
            val player = (application as RadioClientApp).radioPlayer

            when {
                mediaId == "channel_all" -> player?.selectChannel(null)
                mediaId.startsWith("channel_") -> {
                    val channelId = mediaId.removePrefix("channel_").toIntOrNull()
                    player?.selectChannel(channelId)
                }
            }

            // Don't hand items to ExoPlayer — RadioPlayer manages its own queue
            return Futures.immediateFuture(
                MediaSession.MediaItemsWithStartPosition(emptyList(), 0, 0L)
            )
        }
    }

    override fun onCreate() {
        super.onCreate()
        val app = application as RadioClientApp

        val exoPlayer = ExoPlayer.Builder(this)
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                    .setUsage(C.USAGE_MEDIA)
                    .build(),
                /* handleAudioFocus = */ true,
            )
            .setHandleAudioBecomingNoisy(true)
            .build()

        mediaLibrarySession = MediaLibrarySession.Builder(this, exoPlayer, callback)
            .build()

        app.initializePlayer(exoPlayer)
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaLibrarySession? =
        mediaLibrarySession

    override fun onTaskRemoved(rootIntent: Intent?) {
        val player = mediaLibrarySession?.player
        if (player == null || !player.playWhenReady || player.mediaItemCount == 0) {
            stopSelf()
        }
    }

    override fun onDestroy() {
        mediaLibrarySession?.run {
            player.release()
            release()
        }
        mediaLibrarySession = null
        super.onDestroy()
    }
}
