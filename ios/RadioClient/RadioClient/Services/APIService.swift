import Foundation

class APIService: ObservableObject {
    @Published var serverURL: String {
        didSet { UserDefaults.standard.set(serverURL, forKey: "serverURL") }
    }
    @Published var apiKey: String {
        didSet { UserDefaults.standard.set(apiKey, forKey: "apiKey") }
    }

    init() {
        self.serverURL = UserDefaults.standard.string(forKey: "serverURL") ?? ""
        self.apiKey = UserDefaults.standard.string(forKey: "apiKey") ?? ""
    }

    var isConfigured: Bool {
        !serverURL.isEmpty && !apiKey.isEmpty
    }

    private var baseURL: URL? {
        let host = serverURL.hasPrefix("http") ? serverURL : "http://\(serverURL)"
        return URL(string: host)
    }

    private func authorizedRequest(url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        return request
    }

    func sync(played: [PlayedSong], bufferCacheMB: Int = 100) async throws -> [SongItem] {
        guard let base = baseURL else { throw APIError.invalidURL }
        let url = base.appendingPathComponent("/library/api/client_sync/")

        var request = authorizedRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let playedData = played.map { entry in
            ["id": entry.song.id, "played_at": formatter.string(from: entry.playedAt)] as [String: Any]
        }
        let body: [String: Any] = ["played": playedData, "buffer_cache_mb": bufferCacheMB]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            throw APIError.serverError(code)
        }
        let syncResponse = try JSONDecoder().decode(SyncResponse.self, from: data)
        return syncResponse.download
    }

    func downloadSong(playlistItemId: Int) async throws -> URL {
        let cache = CacheManager.shared
        if cache.hasCached(playlistItemId: playlistItemId) {
            return cache.fileURL(for: playlistItemId)
        }

        guard let base = baseURL else { throw APIError.invalidURL }
        let url = base.appendingPathComponent("/library/api/download_song/\(playlistItemId)/")
        let request = authorizedRequest(url: url)

        let (tempURL, response) = try await URLSession.shared.download(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            throw APIError.serverError(code)
        }

        let dest = cache.fileURL(for: playlistItemId)
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.moveItem(at: tempURL, to: dest)
        return dest
    }

    func coverArtURL(albumId: Int) -> URL? {
        guard let base = baseURL else { return nil }
        return base.appendingPathComponent("/library/cover/\(albumId)/")
    }

    func testConnection() async -> Result<Int, Error> {
        do {
            let items = try await sync(played: [], bufferCacheMB: 0)
            return .success(items.count)
        } catch {
            return .failure(error)
        }
    }
}

enum APIError: LocalizedError {
    case invalidURL
    case serverError(Int)

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Invalid server URL"
        case .serverError(let code): return "Server error (\(code))"
        }
    }
}
