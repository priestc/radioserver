import SwiftUI

/// Compact control shown under a channel row in Settings, showing the channel's
/// current cache limit and opening an editor sheet to change it.
struct ChannelCacheLimitWidget: View {
    let channelId: Int?
    let channelName: String

    @ObservedObject private var settings = ChannelCacheSettings.shared
    @State private var showEditor = false

    var body: some View {
        Button {
            showEditor = true
        } label: {
            HStack(spacing: 4) {
                Text("Cache Limit")
                Spacer()
                Text(settings.limit(for: channelId).displayText)
                    .foregroundColor(.accentColor)
                Image(systemName: "chevron.right")
                    .foregroundColor(.secondary)
            }
            .font(.caption)
        }
        .buttonStyle(.plain)
        .sheet(isPresented: $showEditor) {
            ChannelCacheLimitEditor(channelId: channelId, channelName: channelName)
        }
    }
}

private struct ChannelCacheLimitEditor: View {
    let channelId: Int?
    let channelName: String

    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var settings = ChannelCacheSettings.shared

    @State private var mode: CacheLimitMode
    @State private var hours: Int
    @State private var minutes: Int
    @State private var gigabytes: Double

    init(channelId: Int?, channelName: String) {
        self.channelId = channelId
        self.channelName = channelName
        let current = ChannelCacheSettings.shared.limit(for: channelId)
        _mode = State(initialValue: current.mode)
        let totalMinutes = Int(current.durationSeconds / 60)
        _hours = State(initialValue: totalMinutes / 60)
        _minutes = State(initialValue: totalMinutes % 60)
        _gigabytes = State(initialValue: current.sizeGB)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section(channelName) {
                    Picker("Limit by", selection: $mode) {
                        Text("Duration").tag(CacheLimitMode.duration)
                        Text("Size").tag(CacheLimitMode.size)
                    }
                    .pickerStyle(.segmented)

                    if mode == .duration {
                        Stepper("Hours: \(hours)", value: $hours, in: 0...24)
                        Stepper("Minutes: \(minutes)", value: $minutes, in: 0...55, step: 5)
                    } else {
                        HStack {
                            Text("Gigabytes")
                            Spacer()
                            TextField("GB", value: $gigabytes, format: .number)
                                .keyboardType(.decimalPad)
                                .multilineTextAlignment(.trailing)
                                .frame(width: 80)
                        }
                    }
                }
            }
            .navigationTitle("Cache Limit")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let limit = ChannelCacheLimit(
                            mode: mode,
                            durationSeconds: Double(hours * 3600 + minutes * 60),
                            sizeGB: gigabytes
                        )
                        settings.setLimit(limit, for: channelId)
                        dismiss()
                    }
                }
            }
        }
    }
}
