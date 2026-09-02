import UIKit

/// Locations for everything the app persists. Every path lives under Application
/// Support, which iOS never purges on its own (unlike Caches). The directory does NOT
/// exist by default in an iOS sandbox, so every accessor here creates it first — a
/// write to a path whose parent is missing fails silently under `try?`, which used to
/// mean "whichever persistence ran first wins, the rest quietly lose their data".
enum AppPaths {
    static var appSupport: URL {
        let fm = FileManager.default
        let dir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fm.temporaryDirectory
        if !fm.fileExists(atPath: dir.path) {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }

    static func file(_ name: String) -> URL {
        appSupport.appendingPathComponent(name)
    }
}

class CacheManager {
    static let shared = CacheManager()

    // CRITICAL: downloaded audio and artwork are stored under Application Support, NOT
    // .cachesDirectory. iOS purges .cachesDirectory freely under storage pressure —
    // especially while the app is backgrounded (phone locked during a long drive) — and
    // a multi-hour music cache is a prime target. A purge mid-trip wiped the file that
    // was playing AND every queued file, and with no signal there was nothing to
    // re-download: exactly the offline-playback failure this cache exists to prevent
    // (see CLAUDE.md). Application Support is not purged by the system; we only delete
    // from it ourselves. The directory is also excluded from iCloud/iTunes backup so the
    // cache doesn't bloat backups.
    //
    // Both directories are created exactly once here. They used to be computed
    // properties that re-ran createDirectory + setResourceValues on EVERY access, and
    // hasCached()/fileSizeBytes() are called hundreds of times per stats recompute.
    private let cacheDir: URL
    private let artworkDir: URL

    init() {
        cacheDir = Self.makeDir("SongCache")
        artworkDir = Self.makeDir("ArtworkCache")
        migrateFromCachesDirectoryIfNeeded()
    }

    private static func makeDir(_ name: String) -> URL {
        var dir = AppPaths.appSupport.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? dir.setResourceValues(values)
        return dir
    }

    /// Re-creates the directories if something external removed them (the app should
    /// never crash or lose the ability to write just because a folder went missing).
    private func ensureDirs() {
        let fm = FileManager.default
        for dir in [cacheDir, artworkDir] where !fm.fileExists(atPath: dir.path) {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }
    }

    /// One-time move of any files left in the old `Caches/SongCache` and
    /// `Caches/ArtworkCache` locations into the new Application Support directories.
    /// Safe to run every launch: once the old directories are empty/gone it does nothing.
    private func migrateFromCachesDirectoryIfNeeded() {
        let fm = FileManager.default
        guard let oldBase = fm.urls(for: .cachesDirectory, in: .userDomainMask).first else { return }
        for name in ["SongCache", "ArtworkCache"] {
            let oldDir = oldBase.appendingPathComponent(name, isDirectory: true)
            guard fm.fileExists(atPath: oldDir.path),
                  let files = try? fm.contentsOfDirectory(at: oldDir, includingPropertiesForKeys: nil) else { continue }
            let newDir = name == "SongCache" ? cacheDir : artworkDir
            var moved = 0
            for file in files {
                let dest = newDir.appendingPathComponent(file.lastPathComponent)
                if fm.fileExists(atPath: dest.path) {
                    try? fm.removeItem(at: file)
                    continue
                }
                do {
                    try fm.moveItem(at: file, to: dest)
                    moved += 1
                } catch {
                    // Leave it; next launch will try again.
                }
            }
            try? fm.removeItem(at: oldDir)
            if moved > 0 {
                AppLogger.shared.log(.startup, "Moved \(moved) cached \(name == "SongCache" ? "song" : "artwork") file\(moved == 1 ? "" : "s") to permanent storage")
            }
        }
    }

    // MARK: - Artwork cache (by album ID)

    func artworkURL(for albumId: Int) -> URL {
        artworkDir.appendingPathComponent("\(albumId).jpg")
    }

    func hasArtwork(for albumId: Int) -> Bool {
        FileManager.default.fileExists(atPath: artworkURL(for: albumId).path)
    }

    func cachedArtwork(for albumId: Int) -> UIImage? {
        let path = artworkURL(for: albumId).path
        guard FileManager.default.fileExists(atPath: path) else { return nil }
        return UIImage(contentsOfFile: path)
    }

    func saveArtwork(_ image: UIImage, for albumId: Int) {
        guard let data = image.jpegData(compressionQuality: 0.8) else { return }
        ensureDirs()
        try? data.write(to: artworkURL(for: albumId), options: .atomic)
    }

    // MARK: - Audio cache (by playlist item ID)

    func fileURL(for playlistItemId: Int, ext: String = "mp3") -> URL {
        cacheDir.appendingPathComponent("\(playlistItemId).\(ext)")
    }

    func hasCached(playlistItemId: Int, ext: String = "mp3") -> Bool {
        FileManager.default.fileExists(atPath: fileURL(for: playlistItemId, ext: ext).path)
    }

    /// The on-disk extension a song is playable from — its native format first, then
    /// the low-bitrate mp3 fallback — or nil if no audio for it is on disk. Every
    /// "is this song playable right now?" decision in the app goes through this.
    func cachedExtension(for song: SongItem) -> String? {
        if hasCached(playlistItemId: song.id, ext: song.fileExtension) { return song.fileExtension }
        if song.fileExtension != "mp3", hasCached(playlistItemId: song.id, ext: "mp3") { return "mp3" }
        return nil
    }

    func isCached(_ song: SongItem) -> Bool {
        cachedExtension(for: song) != nil
    }

    /// Prepares the destination for a freshly downloaded file. Returns the URL to move
    /// the temp file to (parent directory guaranteed to exist, any stale copy removed).
    func prepareDestination(for playlistItemId: Int, ext: String) -> URL {
        ensureDirs()
        let dest = fileURL(for: playlistItemId, ext: ext)
        try? FileManager.default.removeItem(at: dest)
        return dest
    }

    func fileSizeBytes(for playlistItemId: Int, ext: String) -> Int64 {
        let url = fileURL(for: playlistItemId, ext: ext)
        guard let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize else { return 0 }
        return Int64(size)
    }

    func fileSizeMB(for playlistItemId: Int, ext: String) -> Double {
        Double(fileSizeBytes(for: playlistItemId, ext: ext)) / (1024 * 1024)
    }

    func cacheFileCount() -> Int {
        (try? FileManager.default.contentsOfDirectory(at: cacheDir, includingPropertiesForKeys: nil))?.count ?? 0
    }

    func totalCacheSizeMB() -> Double {
        dirSizeMB(cacheDir)
    }

    func totalArtworkSizeMB() -> Double {
        dirSizeMB(artworkDir)
    }

    private func dirSizeMB(_ dir: URL) -> Double {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.fileSizeKey]) else {
            return 0
        }
        var total: Int64 = 0
        for file in files {
            if let size = try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize {
                total += Int64(size)
            }
        }
        return Double(total) / (1024 * 1024)
    }

    func removeFile(for playlistItemId: Int, ext: String = "mp3") {
        try? FileManager.default.removeItem(at: fileURL(for: playlistItemId, ext: ext))
    }

    func clearCache() {
        try? FileManager.default.removeItem(at: cacheDir)
        try? FileManager.default.removeItem(at: artworkDir)
        ensureDirs()
    }
}
