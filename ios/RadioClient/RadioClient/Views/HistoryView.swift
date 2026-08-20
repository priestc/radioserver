import SwiftUI
import UIKit

struct HistoryView: View {
    @ObservedObject private var logger = AppLogger.shared
    @State private var showCopyConfirmation = false

    private var visibleEntries: [LogEntry] {
        logger.entries.filter { !$0.kind.isRequest }
    }

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
                    List(visibleEntries) { entry in
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: entry.kind.iconName)
                                .foregroundColor(entry.kind.iconColor)
                                .frame(width: 20)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(entry.message)
                                    .font(.caption)
                                Text(entry.timestamp, style: .relative)
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                            }
                        }
                        .padding(.vertical, 1)
                    }
                    .listStyle(.plain)
                    .textSelection(.enabled)
                }
            }
            .navigationTitle("Log")
            .overlay(alignment: .bottom) {
                if showCopyConfirmation {
                    Text("Copied to clipboard")
                        .font(.caption)
                        .foregroundColor(.white)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(Color.black.opacity(0.75))
                        .clipShape(Capsule())
                        .padding(.bottom, 12)
                }
            }
            .toolbar {
                if !logger.entries.isEmpty {
                    ToolbarItem(placement: .navigationBarTrailing) {
                        Button {
                            copyToClipboard()
                        } label: {
                            Image(systemName: "doc.on.doc")
                        }
                    }
                    ToolbarItem(placement: .navigationBarTrailing) {
                        Button("Clear") { logger.clear() }
                    }
                }
            }
        }
    }

    private func copyToClipboard() {
        UIPasteboard.general.string = logger.formattedText(visibleEntries)
        showCopyConfirmation = true
        Task {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            showCopyConfirmation = false
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
        case .apiRequest:      return ""
        case .apiSuccess:      return "checkmark.circle.fill"
        case .apiFailure:      return "xmark.circle.fill"
        case .startup:         return "power.circle.fill"
        case .cacheState:      return "internaldrive.fill"
        case .playbackError:   return "exclamationmark.triangle.fill"
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
        case .playbackError:   return .red
        }
    }

    var isRequest: Bool { self == .apiRequest }
}
