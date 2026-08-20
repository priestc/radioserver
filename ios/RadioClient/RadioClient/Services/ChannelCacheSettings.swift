import Foundation

enum CacheLimitMode: String, Codable {
    case duration
    case size
}

struct ChannelCacheLimit: Codable, Equatable {
    var mode: CacheLimitMode
    var durationSeconds: Double
    var sizeGB: Double

    static let `default` = ChannelCacheLimit(mode: .duration, durationSeconds: 3600, sizeGB: 1.0)

    var sizeBytes: Int64 {
        Int64(sizeGB * 1024 * 1024 * 1024)
    }

    var displayText: String {
        switch mode {
        case .duration:
            return CacheFormat.duration(durationSeconds)
        case .size:
            return String(format: "%.1f GB", sizeGB)
        }
    }

    /// Upper bound (in MB) to ask the server for when requesting queue metadata for
    /// this channel. Deliberately generous — the client enforces the real limit itself
    /// against actual downloaded file sizes/durations, this just needs to make sure
    /// the server offers enough candidates to reach the target.
    var requestBufferMB: Int {
        switch mode {
        case .size:
            return max(Int(sizeGB * 1024) + 50, 50)
        case .duration:
            let minutes = durationSeconds / 60
            return max(Int(minutes * 10), 50)
        }
    }
}

/// Per-channel cache limits, keyed by channel ID (nil channel = "All Music").
/// Replaces the old single global buffer-size setting.
class ChannelCacheSettings: ObservableObject {
    static let shared = ChannelCacheSettings()

    @Published private var limits: [String: ChannelCacheLimit] = [:] {
        didSet { save() }
    }

    private static let storageKey = "channelCacheLimits"

    private init() {
        load()
    }

    private func key(for channelId: Int?) -> String {
        channelId.map(String.init) ?? "all"
    }

    func limit(for channelId: Int?) -> ChannelCacheLimit {
        limits[key(for: channelId)] ?? .default
    }

    func setLimit(_ limit: ChannelCacheLimit, for channelId: Int?) {
        limits[key(for: channelId)] = limit
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(limits) else { return }
        UserDefaults.standard.set(data, forKey: Self.storageKey)
    }

    private func load() {
        guard let data = UserDefaults.standard.data(forKey: Self.storageKey),
              let decoded = try? JSONDecoder().decode([String: ChannelCacheLimit].self, from: data) else { return }
        limits = decoded
    }
}
