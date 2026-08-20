import SwiftUI

struct ChannelsView: View {
    @EnvironmentObject var apiService: APIService
    @EnvironmentObject var audioPlayer: AudioPlayer

    @State private var channels: [Channel] = []
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationView {
            List {
                Section {
                    channelRow(channel: nil)
                }
                if !channels.isEmpty {
                    Section("Channels") {
                        ForEach(channels) { channel in
                            channelRow(channel: channel)
                        }
                    }
                }
            }
            .navigationTitle("Channels")
            .overlay {
                if isLoading && channels.isEmpty {
                    ProgressView("Loading channels…")
                }
            }
            .overlay(alignment: .bottom) {
                if let msg = errorMessage {
                    Text(msg)
                        .foregroundColor(.white)
                        .padding(10)
                        .background(Color.red.opacity(0.85))
                        .cornerRadius(8)
                        .padding()
                }
            }
            .task {
                await loadChannels()
            }
            .refreshable {
                await loadChannels()
                audioPlayer.refreshCacheStats(reason: "channels pull-to-refresh")
            }
            .onAppear {
                audioPlayer.refreshCacheStats(reason: "channels tab opened")
            }
        }
    }

    @ViewBuilder
    private func channelRow(channel: Channel?) -> some View {
        let isSelected = audioPlayer.selectedChannel == channel
        let isExhausted = audioPlayer.exhaustedChannelIds.contains(channel?.id)
        // cacheUpdateTick is read here so SwiftUI re-evaluates this row after downloads
        let _ = audioPlayer.cacheUpdateTick
        let cacheStats = audioPlayer.cacheStatsPerChannel().first { $0.channelId == channel?.id }
        Button {
            audioPlayer.selectChannel(channel)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .foregroundColor(isSelected ? .accentColor : .secondary)
                    .imageScale(.large)
                VStack(alignment: .leading, spacing: 2) {
                    Text(channel?.name ?? "All Music")
                        .fontWeight(.medium)
                        .foregroundColor(isExhausted ? .secondary : .primary)
                    Text(isExhausted ? "No songs available" : (channel?.subtitle ?? "No filters — plays everything"))
                        .font(.caption)
                        .foregroundColor(.secondary)
                    if let stats = cacheStats {
                        Label(cacheStatusText(stats), systemImage: "arrow.down.circle")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                }
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isExhausted)
    }

    private func cacheStatusText(_ stats: AudioPlayer.ChannelCacheStats) -> String {
        guard stats.songCount > 0 else { return "Nothing cached" }
        let songLabel = stats.songCount == 1 ? "song" : "songs"
        return "\(stats.songCount) \(songLabel) · \(CacheFormat.duration(stats.durationSeconds)) · \(CacheFormat.bytes(stats.sizeBytes))"
    }

    private func loadChannels() async {
        guard apiService.isConfigured else { return }
        isLoading = true
        errorMessage = nil
        do {
            channels = try await apiService.fetchChannels()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}
