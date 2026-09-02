import Foundation

enum LogKind: String, Codable {
    case trackPlayed
    case trackSkipped
    case downloadSuccess
    case downloadFailure
    case apiRequest
    case apiSuccess
    case apiFailure
    case startup
    case cacheState
    case playbackError
}

struct LogEntry: Identifiable, Codable {
    let id: UUID
    let timestamp: Date
    let kind: LogKind
    let message: String
    /// Technical details (URLs, status codes, byte counts, etc.) shown only behind a
    /// "Details" disclosure in the History page — `message` itself should always read
    /// as plain English.
    let details: String?

    init(kind: LogKind, message: String, details: String? = nil) {
        id = UUID()
        timestamp = Date()
        self.kind = kind
        self.message = message
        self.details = details
    }
}

class AppLogger: ObservableObject {
    static let shared = AppLogger()

    @Published private(set) var entries: [LogEntry] = []

    private static let maxEntries = 500
    private var saveTask: Task<Void, Never>?

    private static var logFileURL: URL {
        AppPaths.file("app_log.json")
    }

    init() {
        load()
    }

    func log(_ kind: LogKind, _ message: String, details: String? = nil) {
        let entry = LogEntry(kind: kind, message: message, details: details)
        // Prefixed so it's easy to isolate in Xcode's console filter bar (type "RadioLog"),
        // select all, and copy/paste — quicker than pulling entries off the device.
        let detailsSuffix = details.map { " [\($0)]" } ?? ""
        print("[RadioLog] \(kind.rawValue): \(message)\(detailsSuffix)")
        if Thread.isMainThread {
            insert(entry)
        } else {
            DispatchQueue.main.async { self.insert(entry) }
        }
        scheduleSave()
    }

    /// Plain-text dump of the visible log (oldest first, with details inlined), formatted
    /// for pasting elsewhere.
    func formattedText(_ entries: [LogEntry]) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return entries.reversed().map { entry in
            let detailsSuffix = entry.details.map { " [\($0)]" } ?? ""
            return "[\(formatter.string(from: entry.timestamp))] \(entry.kind.rawValue): \(entry.message)\(detailsSuffix)"
        }.joined(separator: "\n")
    }

    func clear() {
        entries = []
        saveTask?.cancel()
        try? FileManager.default.removeItem(at: Self.logFileURL)
    }

    private func insert(_ entry: LogEntry) {
        entries.insert(entry, at: 0)
        if entries.count > Self.maxEntries {
            entries.removeLast()
        }
    }

    private func scheduleSave() {
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard !Task.isCancelled else { return }
            let snapshot = await MainActor.run { self.entries }
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            if let data = try? encoder.encode(snapshot) {
                try? data.write(to: Self.logFileURL, options: .atomic)
            }
        }
    }

    private func load() {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let data = try? Data(contentsOf: Self.logFileURL),
              let saved = try? decoder.decode([LogEntry].self, from: data) else { return }
        entries = saved
    }
}
