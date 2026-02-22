import Foundation

class CacheManager {
    static let shared = CacheManager()

    private var cacheDir: URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SongCache", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    func fileURL(for playlistItemId: Int, ext: String = "mp3") -> URL {
        cacheDir.appendingPathComponent("\(playlistItemId).\(ext)")
    }

    func hasCached(playlistItemId: Int, ext: String = "mp3") -> Bool {
        FileManager.default.fileExists(atPath: fileURL(for: playlistItemId, ext: ext).path)
    }

    func totalCacheSizeMB() -> Double {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: cacheDir, includingPropertiesForKeys: [.fileSizeKey]) else {
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
    }
}
