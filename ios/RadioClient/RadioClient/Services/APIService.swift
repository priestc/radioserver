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
    @Published var isOnLocalNetwork = false

    private let networkMonitor = NWPathMonitor()

    // URLSession that re-adds the Authorization header when the server redirects,
    // which URLSession.shared strips by default as a security measure.
    private lazy var downloadSession: URLSession = {
        URLSession(configuration: .default, delegate: RedirectPreservingAuth(), delegateQueue: nil)
    }()

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

        startNetworkMonitor()
    }

    private func startNetworkMonitor() {
        // isOnLocalNetwork defaults to false, and NWPathMonitor's first real reading
        // only arrives asynchronously via pathUpdateHandler below — reading
        // currentPath synchronously right after start() doesn't help, since the
        // underlying network evaluation hasn't actually run yet at that point (it
        // reports unsatisfied/default regardless of the real network state). Without
        // this wait, the very first request (e.g. the channels fetch at launch) can
        // fire before any of that is known, wrongly pick the remote URL for a phone
        // that's actually on Wi-Fi, and time out hitting a Tailscale/remote address.
        // So: block briefly (bounded) for the first real callback before continuing.
        let firstReading = DispatchSemaphore(value: 0)
        var sawFirstReading = false
        var initialOnWifi = false
        networkMonitor.pathUpdateHandler = { [weak self] path in
            let onWifi = path.usesInterfaceType(.wifi)
            // Capture the value on this (background) queue and signal immediately —
            // don't rely on the DispatchQueue.main.async below to beat the wait() call
            // further down, since that dispatch can't run until the main thread (which
            // is what's blocked on the wait) is free again.
            if !sawFirstReading {
                sawFirstReading = true
                initialOnWifi = onWifi
                firstReading.signal()
            }
            DispatchQueue.main.async {
                self?.isOnLocalNetwork = onWifi
            }
        }
        networkMonitor.start(queue: DispatchQueue.global(qos: .utility))
        if firstReading.wait(timeout: .now() + 1.0) == .success {
            isOnLocalNetwork = initialOnWifi
        }
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

    private func bodySnippet(_ data: Data) -> String {
        let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !text.isEmpty else { return "" }
        return " — " + (text.count > 200 ? String(text.prefix(200)) + "…" : text)
    }

    func fetchChannels() async throws -> [Channel] {
        guard let base = baseURL else { throw APIError.invalidURL }
        let url = base.appendingPathComponent("/library/api/channels/")
        var request = authorizedRequest(url: url)
        request.timeoutInterval = 10
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                AppLogger.shared.log(.apiFailure, "Couldn't load channels", details: "GET \(url.absoluteString) → \(code)\(bodySnippet(data))")
                throw APIError.serverError(code)
            }
            let channels = try JSONDecoder().decode(ChannelsResponse.self, from: data).channels
            AppLogger.shared.log(.apiSuccess, "Loaded \(channels.count) channels", details: "GET \(url.absoluteString) → 200")
            return channels
        } catch let err as APIError {
            throw err
        } catch {
            if !error.isCancellation {
                AppLogger.shared.log(.apiFailure, "Couldn't load channels — \(error.localizedDescription)", details: "GET \(url.absoluteString)")
            }
            throw error
        }
    }

    func sync(played: [PlayedSong], bufferCacheMB: Int = 100, nowPlaying: (id: Int, startedAt: Date)? = nil, channelId: Int? = nil, targetDurationSeconds: Double? = nil, reason: String, silent: Bool = false) async throws -> [SongItem] {
        guard let base = baseURL else { throw APIError.invalidURL }
        let url = base.appendingPathComponent("/library/api/client_sync/")

        var request = authorizedRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10

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
        if let target = targetDurationSeconds {
            body["target_duration_seconds"] = target
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let playedDesc = played.isEmpty ? "" : " (\(played.count) played)"
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if !silent {
                    AppLogger.shared.log(.apiFailure, "Sync failed", details: "trigger: \(reason); POST \(url.absoluteString) → \(code)\(bodySnippet(data))\(playedDesc)")
                }
                throw APIError.serverError(code)
            }
            // No success log here on purpose: the response's item count is how many
            // tracks the server is willing to hand out this call (bounded by the cache
            // buffer), not how many are genuinely new — that would be misleading. The
            // caller (AudioPlayer) knows the real "new to us" count after diffing
            // against its local queue, and logs that instead.
            let syncResponse = try JSONDecoder().decode(SyncResponse.self, from: data)
            return syncResponse.download
        } catch let err as APIError {
            throw err
        } catch {
            // A cancelled sync just means a newer one superseded it — normal, expected
            // behavior, not an error worth logging (the caller decides whether to retry).
            if !silent && !error.isCancellation {
                AppLogger.shared.log(.apiFailure, "Sync failed — \(error.localizedDescription)", details: "trigger: \(reason); POST \(url.absoluteString)\(playedDesc)")
            }
            throw error
        }
    }

    func downloadSong(playlistItemId: Int, fileExtension: String = "mp3", lowBitrate: Bool = false, silent: Bool = false) async throws -> URL {
        let cache = CacheManager.shared
        let ext = lowBitrate ? "mp3" : fileExtension
        if cache.hasCached(playlistItemId: playlistItemId, ext: ext) {
            return cache.fileURL(for: playlistItemId, ext: ext)
        }

        guard let base = baseURL else { throw APIError.invalidURL }
        let endpoint = lowBitrate ? "download_song_lowbitrate" : "download_song"
        let url = base.appendingPathComponent("/library/api/\(endpoint)/\(playlistItemId)/")
        var request = authorizedRequest(url: url)
        request.timeoutInterval = 30

        do {
            let (tempURL, response) = try await downloadSession.download(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                let body = (try? Data(contentsOf: tempURL)).map { bodySnippet($0) } ?? ""
                if !silent {
                    AppLogger.shared.log(.apiFailure, "Download failed", details: "GET \(url.absoluteString) → \(code)\(body)")
                }
                throw APIError.serverError(code)
            }

            // Guard against truncated transfers (dropped connection, server restart mid-response,
            // device disk full while writing) — if the server declared a Content-Length, make sure
            // we actually received that many bytes before trusting this file as a valid cache entry.
            // Always logged (even when silent) since this is a genuine error condition worth
            // surfacing on its own, not just folded into a batch summary count.
            if let expectedStr = http.value(forHTTPHeaderField: "Content-Length"),
               let expected = Int64(expectedStr) {
                let attrs = try? FileManager.default.attributesOfItem(atPath: tempURL.path)
                let actual = (attrs?[.size] as? NSNumber)?.int64Value ?? -1
                if actual != expected {
                    try? FileManager.default.removeItem(at: tempURL)
                    AppLogger.shared.log(.downloadFailure, "Download was cut off partway through", details: "GET \(url.absoluteString) — expected \(expected)B, got \(actual)B")
                    throw APIError.incompleteDownload(expected: expected, actual: actual)
                }
            }

            let dest = cache.fileURL(for: playlistItemId, ext: ext)
            try? FileManager.default.removeItem(at: dest)
            try FileManager.default.moveItem(at: tempURL, to: dest)
            if !silent {
                AppLogger.shared.log(.apiSuccess, "Downloaded track", details: "GET \(url.absoluteString) → 200")
            }
            return dest
        } catch let err as APIError {
            throw err
        } catch {
            if !silent && !error.isCancellation {
                AppLogger.shared.log(.apiFailure, "Download failed — \(error.localizedDescription)", details: "GET \(url.absoluteString)")
            }
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
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                AppLogger.shared.log(.apiFailure, "Connection test failed", details: "GET \(url.absoluteString) → \(code)\(bodySnippet(data))")
                return .failure(APIError.serverError(code))
            }
            AppLogger.shared.log(.apiSuccess, "Connection test succeeded", details: "GET \(url.absoluteString) → 200")
            return .success(http.statusCode)
        } catch {
            if !error.isCancellation {
                AppLogger.shared.log(.apiFailure, "Connection test failed — \(error.localizedDescription)", details: "GET \(url.absoluteString)")
            }
            return .failure(error)
        }
    }

    deinit {
        networkMonitor.cancel()
    }
}

private class RedirectPreservingAuth: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        var updated = request
        if let auth = task.originalRequest?.value(forHTTPHeaderField: "Authorization") {
            updated.setValue(auth, forHTTPHeaderField: "Authorization")
        }
        completionHandler(updated)
    }
}

enum APIError: LocalizedError {
    case invalidURL
    case serverError(Int)
    case incompleteDownload(expected: Int64, actual: Int64)

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Invalid server URL"
        case .serverError(let code): return "Server error (\(code))"
        case .incompleteDownload(let expected, let actual): return "Incomplete download (expected \(expected) bytes, got \(actual))"
        }
    }
}
