package com.example.radioclient.service

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.File

class CacheManager(context: Context) {
    private val songCacheDir = File(context.cacheDir, "SongCache").also { it.mkdirs() }
    private val artworkCacheDir = File(context.cacheDir, "ArtworkCache").also { it.mkdirs() }

    // Song cache

    fun songFile(playlistItemId: Int, ext: String = "mp3"): File =
        File(songCacheDir, "$playlistItemId.$ext")

    fun hasCachedSong(playlistItemId: Int, ext: String = "mp3"): Boolean =
        songFile(playlistItemId, ext).exists()

    fun removeSong(playlistItemId: Int, ext: String = "mp3") {
        songFile(playlistItemId, ext).delete()
    }

    fun saveSong(playlistItemId: Int, ext: String, data: ByteArray) {
        songFile(playlistItemId, ext).writeBytes(data)
    }

    // Artwork cache

    fun artworkFile(albumId: Int): File =
        File(artworkCacheDir, "$albumId.jpg")

    fun hasCachedArtwork(albumId: Int): Boolean =
        artworkFile(albumId).exists()

    fun cachedArtwork(albumId: Int): Bitmap? {
        val file = artworkFile(albumId)
        if (!file.exists()) return null
        return BitmapFactory.decodeFile(file.absolutePath)
    }

    fun saveArtwork(albumId: Int, data: ByteArray) {
        artworkFile(albumId).writeBytes(data)
    }

    // Size / cleanup

    fun totalSongCacheSizeMB(): Double =
        dirSizeMB(songCacheDir)

    fun totalArtworkCacheSizeMB(): Double =
        dirSizeMB(artworkCacheDir)

    fun clearCache() {
        songCacheDir.listFiles()?.forEach { it.delete() }
        artworkCacheDir.listFiles()?.forEach { it.delete() }
    }

    private fun dirSizeMB(dir: File): Double {
        val bytes = dir.listFiles()?.sumOf { it.length() } ?: 0L
        return bytes / (1024.0 * 1024.0)
    }
}
