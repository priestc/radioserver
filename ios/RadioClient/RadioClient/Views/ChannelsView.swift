import SwiftUI

struct ChannelsView: View {
    @EnvironmentObject var audioPlayer: AudioPlayer

    var body: some View {
        NavigationView {
            List {
                Section {
                    channelRow(channel: nil)
                }
                if !audioPlayer.availableChannels.isEmpty {
                    Section("Channels") {
                        ForEach(audioPlayer.availableChannels) { channel in
                            channelRow(channel: channel)
                        }
                    }
                }
            }
            .navigationTitle("Channels")
            .overlay {
                if audioPlayer.availableChannels.isEmpty && audioPlayer.channelsError == nil {
                    ProgressView("Loading channels…")
                }
            }
            .overlay(alignment: .bottom) {
                if let msg = audioPlayer.channelsError {
                    Text(msg)
                        .foregroundColor(.white)
                        .padding(10)
                        .background(Color.red.opacity(0.85))
                        .cornerRadius(8)
                        .padding()
                }
            }
            .refreshable {
                // fetchChannels is the single owner of this request — calling it here
                // (rather than fetching independently) avoids doubling up with the
                // app-launch fetch or any other concurrent trigger.
                audioPlayer.fetchChannels()
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
        // cachedChannelStats is a precomputed cache (see AudioPlayer.recalculateCacheStats) —
        // just an in-memory lookup, no disk reads on every row/render.
        let cacheStats = audioPlayer.cachedChannelStats.first { $0.channelId == channel?.id }
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

}
