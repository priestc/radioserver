import Foundation

class CacheManager {
    static let shared = CacheManager()

    private var cacheDir: URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SongCache", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    func fileURL(for playlistItemId: Int) -> URL {
        cacheDir.appendingPathComponent("\(playlistItemId).audio")
    }

    func hasCached(playlistItemId: Int) -> Bool {
        FileManager.default.fileExists(atPath: fileURL(for: playlistItemId).path)
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

    func removeFile(for playlistItemId: Int) {
        try? FileManager.default.removeItem(at: fileURL(for: playlistItemId))
    }

    func clearCache() {
        try? FileManager.default.removeItem(at: cacheDir)
        try? FileManager.default.createDirectory(at: cacheDir, withIntermediateDirectories: true)
    }
}
