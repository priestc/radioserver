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
}

struct LogEntry: Identifiable, Codable {
    let id: UUID
    let timestamp: Date
    let kind: LogKind
    let message: String

    init(kind: LogKind, message: String) {
        id = UUID()
        timestamp = Date()
        self.kind = kind
        self.message = message
    }
}

class AppLogger: ObservableObject {
    static let shared = AppLogger()

    @Published private(set) var entries: [LogEntry] = []

    private static let maxEntries = 500
    private var saveTask: Task<Void, Never>?

    private static var logFileURL: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return dir.appendingPathComponent("app_log.json")
    }

    init() {
        load()
    }

    func log(_ kind: LogKind, _ message: String) {
        let entry = LogEntry(kind: kind, message: message)
        if Thread.isMainThread {
            insert(entry)
        } else {
            DispatchQueue.main.async { self.insert(entry) }
        }
        scheduleSave()
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
