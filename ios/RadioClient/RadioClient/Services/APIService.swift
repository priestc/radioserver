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

    func fetchChannels() async throws -> [Channel] {
        guard let base = baseURL else { throw APIError.invalidURL }
        let url = base.appendingPathComponent("/library/api/channels/")
        let request = authorizedRequest(url: url)
        AppLogger.shared.log(.apiRequest, "GET \(url.absoluteString)")
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                AppLogger.shared.log(.apiFailure, "GET \(url.absoluteString) → \(code)")
                throw APIError.serverError(code)
            }
            let channels = try JSONDecoder().decode(ChannelsResponse.self, from: data).channels
            AppLogger.shared.log(.apiSuccess, "GET \(url.absoluteString) → 200 (\(channels.count) channels)")
            return channels
        } catch let err as APIError {
            throw err
        } catch {
            AppLogger.shared.log(.apiFailure, "GET \(url.absoluteString) → \(error.localizedDescription)")
            throw error
        }
    }

    func sync(played: [PlayedSong], bufferCacheMB: Int = 100, nowPlaying: (id: Int, startedAt: Date)? = nil, channelId: Int? = nil) async throws -> [SongItem] {
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
        if let cid = channelId {
            body["channel_id"] = cid
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let playedDesc = played.isEmpty ? "" : " (\(played.count) played)"
        AppLogger.shared.log(.apiRequest, "POST \(url.absoluteString)\(playedDesc)")
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                AppLogger.shared.log(.apiFailure, "POST \(url.absoluteString) → \(code)")
                throw APIError.serverError(code)
            }
            let syncResponse = try JSONDecoder().decode(SyncResponse.self, from: data)
            AppLogger.shared.log(.apiSuccess, "POST \(url.absoluteString) → 200 (\(syncResponse.download.count) to download)")
            return syncResponse.download
        } catch let err as APIError {
            throw err
        } catch {
            AppLogger.shared.log(.apiFailure, "POST \(url.absoluteString) → \(error.localizedDescription)")
            throw error
        }
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

        AppLogger.shared.log(.apiRequest, "GET \(url.absoluteString)")
        do {
            let (tempURL, response) = try await URLSession.shared.download(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                AppLogger.shared.log(.apiFailure, "GET \(url.absoluteString) → \(code)")
                throw APIError.serverError(code)
            }

            let dest = cache.fileURL(for: playlistItemId, ext: ext)
            try? FileManager.default.removeItem(at: dest)
            try FileManager.default.moveItem(at: tempURL, to: dest)
            AppLogger.shared.log(.apiSuccess, "GET \(url.absoluteString) → 200")
            return dest
        } catch let err as APIError {
            throw err
        } catch {
            AppLogger.shared.log(.apiFailure, "GET \(url.absoluteString) → \(error.localizedDescription)")
            throw error
        }
    }

    func coverArtURL(albumId: Int) -> URL? {
        guard let base = baseURL else { return nil }
        return base.appendingPathComponent("/library/cover/\(albumId)/")
    }

    func testConnection() async -> Result<Int, Error> {
        guard let base = baseURL else { return .failure(APIError.invalidURL) }
        let url = base.appendingPathComponent("/library/api/channels/")
        var request = authorizedRequest(url: url)
        request.timeoutInterval = 5
        AppLogger.shared.log(.apiRequest, "GET \(url.absoluteString) (connection test)")
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                AppLogger.shared.log(.apiFailure, "GET \(url.absoluteString) (connection test) → \(code)")
                return .failure(APIError.serverError(code))
            }
            AppLogger.shared.log(.apiSuccess, "GET \(url.absoluteString) (connection test) → 200")
            return .success(http.statusCode)
        } catch {
            AppLogger.shared.log(.apiFailure, "GET \(url.absoluteString) (connection test) → \(error.localizedDescription)")
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
