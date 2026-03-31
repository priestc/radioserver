import Foundation
import Network

class APIService: ObservableObject {
    static let shared = APIService()
    @Published var localURL: String {
        didSet { UserDefaults.standard.set(localURL, forKey: "localURL") }
    }
    @Published var remoteURL: String {
        didSet { UserDefaults.standard.set(remoteURL, forKey: "remoteURL") }
    }
    @Published var apiKey: String {
        didSet { UserDefaults.standard.set(apiKey, forKey: "apiKey") }
    }
    @Published var bufferCacheMB: Int {
        didSet { UserDefaults.standard.set(bufferCacheMB, forKey: "bufferCacheMB") }
    }
    @Published var isOnLocalNetwork = false

    private let networkMonitor = NWPathMonitor()

    init() {
        // Migrate old serverURL to localURL
        if let old = UserDefaults.standard.string(forKey: "serverURL"), !old.isEmpty {
            self.localURL = old
            UserDefaults.standard.removeObject(forKey: "serverURL")
            UserDefaults.standard.set(old, forKey: "localURL")
        } else {
            self.localURL = UserDefaults.standard.string(forKey: "localURL") ?? ""
        }
        self.remoteURL = UserDefaults.standard.string(forKey: "remoteURL") ?? ""
        self.apiKey = UserDefaults.standard.string(forKey: "apiKey") ?? ""
        let saved = UserDefaults.standard.integer(forKey: "bufferCacheMB")
        self.bufferCacheMB = saved > 0 ? saved : 100

        startNetworkMonitor()
    }

    private func startNetworkMonitor() {
        networkMonitor.pathUpdateHandler = { [weak self] path in
            let onWifi = path.usesInterfaceType(.wifi)
            DispatchQueue.main.async {
                self?.isOnLocalNetwork = onWifi
            }
        }
        networkMonitor.start(queue: DispatchQueue.global(qos: .utility))
    }

    var isConfigured: Bool {
        !activeServerURL.isEmpty && !apiKey.isEmpty
    }

    var activeServerURL: String {
        isOnLocalNetwork ? localURL : remoteURL
    }

    private var baseURL: URL? {
        let host = activeServerURL
        guard !host.isEmpty else { return nil }
        let urlString = host.hasPrefix("http") ? host : "http://\(host)"
        guard var components = URLComponents(string: urlString) else { return nil }
        if components.port == nil {
            components.port = 9437
        }
        return components.url
    }

    private func authorizedRequest(url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        return request
    }

    func sync(played: [PlayedSong], bufferCacheMB: Int = 100, nowPlaying: (id: Int, startedAt: Date)? = nil) async throws -> [SongItem] {
        guard let base = baseURL else { throw APIError.invalidURL }
        let url = base.appendingPathComponent("/library/api/client_sync/")

        var request = authorizedRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let playedData = played.map { entry in
            ["id": entry.song.id, "played_at": formatter.string(from: entry.playedAt), "skipped": entry.skipped] as [String: Any]
        }
        var body: [String: Any] = ["played": playedData, "buffer_cache_mb": bufferCacheMB]
        if let np = nowPlaying {
            body["now_playing"] = ["id": np.id, "started_at": formatter.string(from: np.startedAt)]
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            throw APIError.serverError(code)
        }
        let syncResponse = try JSONDecoder().decode(SyncResponse.self, from: data)
        return syncResponse.download
    }

    func downloadSong(playlistItemId: Int, fileExtension: String = "mp3", lowBitrate: Bool = false) async throws -> URL {
        let cache = CacheManager.shared
        let ext = lowBitrate ? "mp3" : fileExtension
        if cache.hasCached(playlistItemId: playlistItemId, ext: ext) {
            return cache.fileURL(for: playlistItemId, ext: ext)
        }

        guard let base = baseURL else { throw APIError.invalidURL }
        let endpoint = lowBitrate ? "download_song_lowbitrate" : "download_song"
        let url = base.appendingPathComponent("/library/api/\(endpoint)/\(playlistItemId)/")
        let request = authorizedRequest(url: url)

        let (tempURL, response) = try await URLSession.shared.download(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            throw APIError.serverError(code)
        }

        let dest = cache.fileURL(for: playlistItemId, ext: ext)
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

    deinit {
        networkMonitor.cancel()
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
