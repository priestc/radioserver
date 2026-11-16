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
    /// Off by default: new songs only download while talking to the local server, so
    /// being away from home never burns cellular data or a hotspot on music downloads.
    @Published var allowRemoteDownloads: Bool {
        didSet { UserDefaults.standard.set(allowRemoteDownloads, forKey: "allowRemoteDownloads") }
    }

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
        self.allowRemoteDownloads = UserDefaults.standard.bool(forKey: "allowRemoteDownloads")

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
            // Being on Wi-Fi is only a first guess — it may be someone else's Wi-Fi,
            // where the LAN address is unreachable and only Tailscale works. The probe
            // below confirms by actually contacting the local server.
            DispatchQueue.main.async {
                self?.isOnLocalNetwork = onWifi
                self?.scheduleProbe()
            }
        }
        networkMonitor.start(queue: DispatchQueue.global(qos: .utility))
        if firstReading.wait(timeout: .now() + 1.0) == .success {
            isOnLocalNetwork = initialOnWifi
        }
    }

    // MARK: - Local / remote route selection

    private var probeTask: Task<Void, Never>?

    /// Re-checks which server is reachable, debounced: NWPathMonitor tends to fire
    /// several updates in a row (e.g. Wi-Fi joins, then the Tailscale VPN reconnects).
    private func scheduleProbe() {
        probeTask?.cancel()
        probeTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard !Task.isCancelled else { return }
            await self?.probeRoute()
        }
    }

    /// Decides between the local and remote URL by actually trying the local server
    /// with a short timeout. Any HTTP response at all (even a 401) means it's reachable.
    func probeRoute() async {
        guard let local = Self.makeBase(localURL), Self.makeBase(remoteURL) != nil else {
            // Only one URL configured — activeServerURL already falls back to it.
            return
        }
        let reachable = await isReachable(local, timeout: 2)
        if Task.isCancelled { return }
        await setRoute(local: reachable, reason: reachable ? "local server answered" : "local server unreachable")
    }

    private func isReachable(_ base: URL, timeout: TimeInterval) async -> Bool {
        var request = authorizedRequest(url: base.appendingPathComponent("/library/api/channels/"))
        request.timeoutInterval = timeout
        request.httpMethod = "HEAD"
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            return response is HTTPURLResponse
        } catch {
            return false
        }
    }

    @MainActor
    private func setRoute(local: Bool, reason: String) {
        guard isOnLocalNetwork != local else { return }
        isOnLocalNetwork = local
        NotificationCenter.default.post(name: .serverRouteChanged, object: self)
        AppLogger.shared.log(.apiSuccess, "Switched to \(local ? "local" : "remote (Tailscale)") server — \(reason)", details: activeServerURL)
    }

    /// Errors meaning the chosen server couldn't be reached at all — worth trying the
    /// other URL. `definite` = the request certainly never reached the server, so even
    /// a non-idempotent POST is safe to resend.
    private static func routeFailure(_ error: Error) -> (eligible: Bool, definite: Bool) {
        guard let err = error as? URLError else { return (false, false) }
        switch err.code {
        case .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
            return (true, true)
        case .timedOut, .networkConnectionLost:
            return (true, false)
        default:
            return (false, false)
        }
    }

    /// Runs `op` against the active server; if that server can't be reached, switches
    /// to the other one (local ⇄ Tailscale) and retries there. Non-idempotent requests
    /// are only resent when the first attempt definitely never reached the server;
    /// otherwise the route is still switched so the caller's next retry goes the right way.
    private func withFailover<T>(idempotent: Bool, _ op: (URL) async throws -> T) async throws -> T {
        guard let primary = baseURL else { throw APIError.invalidURL }
        do {
            return try await op(primary)
        } catch {
            let failure = Self.routeFailure(error)
            guard failure.eligible, !error.isCancellation,
                  let alternate = alternateBaseURL, baseURL == primary else { throw error }
            let switchingToLocal = !isOnLocalNetwork
            guard idempotent || failure.definite else {
                if await isReachable(alternate, timeout: 3) {
                    await setRoute(local: switchingToLocal, reason: "\(primary.host ?? "server") unreachable")
                }
                throw error
            }
            await setRoute(local: switchingToLocal, reason: "\(primary.host ?? "server") unreachable")
            return try await op(alternate)
        }
    }

    var isConfigured: Bool {
        !activeServerURL.isEmpty && !apiKey.isEmpty
    }

    /// True when requests are actually going to the local server (not just "on Wi-Fi").
    var isUsingLocalServer: Bool {
        guard let local = Self.makeBase(localURL) else { return false }
        return baseURL == local
    }

    /// Whether new songs may be downloaded right now.
    var canDownloadSongs: Bool {
        isUsingLocalServer || allowRemoteDownloads
    }

    var activeServerURL: String {
        let preferred = isOnLocalNetwork ? localURL : remoteURL
        if !preferred.isEmpty { return preferred }
        // Fall back to whichever URL is configured. A half-configured setup (e.g. only
        // the remote/Tailscale URL filled in) must still be able to sync — otherwise
        // being on Wi-Fi with a blank localURL leaves the app permanently "not set up"
        // and unable to recover an empty cache.
        return isOnLocalNetwork ? remoteURL : localURL
    }

    private var baseURL: URL? { Self.makeBase(activeServerURL) }

    /// The URL that isn't currently active — what we fail over to.
    private var alternateBaseURL: URL? {
        let alt = Self.makeBase(isOnLocalNetwork ? remoteURL : localURL)
        return alt == baseURL ? nil : alt
    }

    private static func makeBase(_ host: String) -> URL? {
        let host = host.trimmingCharacters(in: .whitespacesAndNewlines)
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
        try await withFailover(idempotent: true) { base in try await self.fetchChannels(base: base) }
    }

    private func fetchChannels(base: URL) async throws -> [Channel] {
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
        try await withFailover(idempotent: false) { base in
            try await self.sync(base: base, played: played, bufferCacheMB: bufferCacheMB, nowPlaying: nowPlaying, channelId: channelId, targetDurationSeconds: targetDurationSeconds, reason: reason, silent: silent)
        }
    }

    private func sync(base: URL, played: [PlayedSong], bufferCacheMB: Int, nowPlaying: (id: Int, startedAt: Date)?, channelId: Int?, targetDurationSeconds: Double?, reason: String, silent: Bool) async throws -> [SongItem] {
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

    /// Asks the server to delete every unplayed playlist item for these channels (except
    /// `keepId`, the song currently playing) and generate fresh ones. `played` is
    /// reported first so those songs land in history rather than being deleted.
    func resetQueue(channelIds: [Int?], played: [PlayedSong], keepId: Int?) async throws {
        try await withFailover(idempotent: false) { base in
            try await self.resetQueue(base: base, channelIds: channelIds, played: played, keepId: keepId)
        }
    }

    private func resetQueue(base: URL, channelIds: [Int?], played: [PlayedSong], keepId: Int?) async throws {
        let url = base.appendingPathComponent("/library/api/reset_queue/")

        var request = authorizedRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Generating a fresh hour for every channel takes a while on a big library.
        request.timeoutInterval = 120

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let playedData = played.map { entry in
            ["id": entry.song.id, "played_at": formatter.string(from: entry.playedAt), "skipped": entry.skipped] as [String: Any]
        }
        var body: [String: Any] = [
            "channel_ids": channelIds.map { $0.map { $0 as Any } ?? NSNull() },
            "played": playedData,
        ]
        if let keepId {
            body["keep_id"] = keepId
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                AppLogger.shared.log(.apiFailure, "Queue reset failed", details: "POST \(url.absoluteString) → \(code)\(bodySnippet(data))")
                throw APIError.serverError(code)
            }
            AppLogger.shared.log(.apiSuccess, "Server reset the queue for \(channelIds.count) channel\(channelIds.count == 1 ? "" : "s")", details: "POST \(url.absoluteString) → 200")
        } catch let err as APIError {
            throw err
        } catch {
            if !error.isCancellation {
                AppLogger.shared.log(.apiFailure, "Queue reset failed — \(error.localizedDescription)", details: "POST \(url.absoluteString)")
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
        guard canDownloadSongs else { throw APIError.remoteDownloadsDisabled }
        return try await withFailover(idempotent: true) { base in
            // Failing over from local to remote mustn't sneak a download past the setting.
            guard base == Self.makeBase(self.localURL) || self.allowRemoteDownloads else {
                throw APIError.remoteDownloadsDisabled
            }
            return try await self.downloadSong(base: base, playlistItemId: playlistItemId, ext: ext, lowBitrate: lowBitrate, silent: silent)
        }
    }

    private func downloadSong(base: URL, playlistItemId: Int, ext: String, lowBitrate: Bool, silent: Bool) async throws -> URL {
        let cache = CacheManager.shared
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

            let dest = cache.prepareDestination(for: playlistItemId, ext: ext)
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

    /// Fetches cover art with a short timeout. Artwork is purely cosmetic, so this must
    /// never be allowed to hold up a download loop — the default 60s URLSession timeout
    /// per album, multiplied by a queue of songs, used to stall prefill for the better
    /// part of an hour whenever the server was unreachable but the network was "up".
    func fetchCoverArt(albumId: Int) async throws -> (Data, Int) {
        guard let url = coverArtURL(albumId: albumId) else { throw APIError.invalidURL }
        var request = authorizedRequest(url: url)
        request.timeoutInterval = 8
        let (data, response) = try await URLSession.shared.data(for: request)
        return (data, (response as? HTTPURLResponse)?.statusCode ?? 0)
    }

    func testConnection() async -> Result<Int, Error> {
        // Re-check which server is reachable first, so the test reflects what the app
        // would actually use right now rather than a stale guess.
        await probeRoute()
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
    case remoteDownloadsDisabled

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Invalid server URL"
        case .serverError(let code): return "Server error (\(code))"
        case .incompleteDownload(let expected, let actual): return "Incomplete download (expected \(expected) bytes, got \(actual))"
        case .remoteDownloadsDisabled: return "Downloads are off away from the local network (enable in Settings)"
        }
    }
}

extension Notification.Name {
    /// Posted when APIService switches between the local and remote server.
    static let serverRouteChanged = Notification.Name("serverRouteChanged")
}
