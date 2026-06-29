import SwiftUI

struct HistoryView: View {
    @ObservedObject private var logger = AppLogger.shared

    var body: some View {
        NavigationStack {
            Group {
                if logger.entries.isEmpty {
                    ContentUnavailableView(
                        "No Activity",
                        systemImage: "clock",
                        description: Text("App events will appear here.")
                    )
                } else {
                    List(logger.entries) { entry in
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: entry.kind.iconName)
                                .foregroundColor(entry.kind.iconColor)
                                .frame(width: 20)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(entry.message)
                                    .font(.caption)
                                    .foregroundColor(entry.kind.isRequest ? .secondary : .primary)
                                Text(entry.timestamp, style: .relative)
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                            }
                        }
                        .padding(.vertical, 1)
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Log")
            .toolbar {
                if !logger.entries.isEmpty {
                    ToolbarItem(placement: .navigationBarTrailing) {
                        Button("Clear") { logger.clear() }
                    }
                }
            }
        }
    }
}

extension LogKind {
    var iconName: String {
        switch self {
        case .trackPlayed:     return "music.note"
        case .trackSkipped:    return "forward.fill"
        case .downloadSuccess: return "arrow.down.circle.fill"
        case .downloadFailure: return "exclamationmark.circle.fill"
        case .apiRequest:      return "arrow.up.circle"
        case .apiSuccess:      return "checkmark.circle.fill"
        case .apiFailure:      return "xmark.circle.fill"
        case .startup:         return "power.circle.fill"
        case .cacheState:      return "internaldrive.fill"
        }
    }

    var iconColor: Color {
        switch self {
        case .trackPlayed:     return .green
        case .trackSkipped:    return .orange
        case .downloadSuccess: return .blue
        case .downloadFailure: return .red
        case .apiRequest:      return .secondary
        case .apiSuccess:      return .green
        case .apiFailure:      return .red
        case .startup:         return .purple
        case .cacheState:      return .teal
        }
    }

    var isRequest: Bool { self == .apiRequest }
}
