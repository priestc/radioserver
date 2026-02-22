import SwiftUI

struct NowPlayingView: View {
    @EnvironmentObject var player: AudioPlayer
    @EnvironmentObject var api: APIService

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                Spacer()

                // Album art
                if let albumId = player.currentSong?.albumId {
                    CoverArtView(albumId: albumId)
                        .frame(width: 280, height: 280)
                        .cornerRadius(12)
                        .shadow(radius: 8)
                } else {
                    RoundedRectangle(cornerRadius: 12)
                        .fill(Color.secondary.opacity(0.2))
                        .frame(width: 280, height: 280)
                        .overlay {
                            Image(systemName: "music.note")
                                .font(.system(size: 60))
                                .foregroundColor(.secondary)
                        }
                }

                // Song info
                VStack(spacing: 6) {
                    Text(player.currentSong?.title ?? "Not Playing")
                        .font(.title2.bold())
                        .lineLimit(2)
                        .multilineTextAlignment(.center)
                    Text(player.currentSong?.artist ?? "")
                        .font(.body)
                        .foregroundColor(.secondary)
                    if let album = player.currentSong?.album {
                        Text(album)
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
                .padding(.horizontal)

                // Progress bar
                VStack(spacing: 4) {
                    ProgressView(value: player.duration > 0 ? player.currentTime / player.duration : 0)
                        .tint(.primary)
                    HStack {
                        Text(formatTime(player.currentTime))
                        Spacer()
                        Text("-\(formatTime(max(0, player.duration - player.currentTime)))")
                    }
                    .font(.caption2)
                    .foregroundColor(.secondary)
                }
                .padding(.horizontal, 32)

                // Controls
                HStack(spacing: 48) {
                    Button(action: { player.togglePlayPause() }) {
                        Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                            .font(.system(size: 56))
                    }
                    Button(action: { player.skipToNext() }) {
                        Image(systemName: "forward.fill")
                            .font(.system(size: 32))
                    }
                }
                .foregroundColor(.primary)

                // Queue info
                Text("\(player.queue.count) songs in queue")
                    .font(.caption)
                    .foregroundColor(.secondary)

                Spacer()
            }
            .navigationTitle("Now Playing")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func formatTime(_ seconds: Double) -> String {
        guard seconds.isFinite && seconds >= 0 else { return "0:00" }
        let mins = Int(seconds) / 60
        let secs = Int(seconds) % 60
        return "\(mins):\(String(format: "%02d", secs))"
    }
}

struct CoverArtView: View {
    let albumId: Int
    @EnvironmentObject var player: AudioPlayer
    @EnvironmentObject var api: APIService
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
            } else {
                Color.secondary.opacity(0.2)
                    .overlay {
                        ProgressView()
                    }
            }
        }
        .task(id: albumId) {
            // Use cached artwork from AudioPlayer if available
            if let cached = player.artworkCache[albumId] {
                image = cached
                return
            }
            guard let artURL = api.coverArtURL(albumId: albumId) else { return }
            var request = URLRequest(url: artURL)
            request.setValue("Bearer \(api.apiKey)", forHTTPHeaderField: "Authorization")
            guard let (data, _) = try? await URLSession.shared.data(for: request),
                  let uiImage = UIImage(data: data) else { return }
            player.artworkCache[albumId] = uiImage
            image = uiImage
        }
    }
}
