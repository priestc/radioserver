import UIKit

class CacheManager {
    static let shared = CacheManager()

    private var cacheDir: URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SongCache", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private var artworkDir: URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("ArtworkCache", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    // MARK: - Artwork cache (by album ID)

    func artworkURL(for albumId: Int) -> URL {
        artworkDir.appendingPathComponent("\(albumId).jpg")
    }

    func cachedArtwork(for albumId: Int) -> UIImage? {
        let path = artworkURL(for: albumId).path
        guard FileManager.default.fileExists(atPath: path) else { return nil }
        return UIImage(contentsOfFile: path)
    }

    func saveArtwork(_ image: UIImage, for albumId: Int) {
        guard let data = image.jpegData(compressionQuality: 0.8) else { return }
        try? data.write(to: artworkURL(for: albumId))
    }

    func fileURL(for playlistItemId: Int, ext: String = "mp3") -> URL {
        cacheDir.appendingPathComponent("\(playlistItemId).\(ext)")
    }

    func hasCached(playlistItemId: Int, ext: String = "mp3") -> Bool {
        FileManager.default.fileExists(atPath: fileURL(for: playlistItemId, ext: ext).path)
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
        try? FileManager.default.createDirectory(at: cacheDir, withIntermediateDirectories: true)
        try? FileManager.default.removeItem(at: artworkDir)
        try? FileManager.default.createDirectory(at: artworkDir, withIntermediateDirectories: true)
    }
}
