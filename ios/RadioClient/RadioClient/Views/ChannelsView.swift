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
            }
        }
    }

    @ViewBuilder
    private func channelRow(channel: Channel?) -> some View {
        let isSelected = audioPlayer.selectedChannel == channel
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
                        .foregroundColor(.primary)
                    Text(channel?.subtitle ?? "No filters — plays everything")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
