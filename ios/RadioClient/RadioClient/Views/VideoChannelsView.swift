import SwiftUI

struct VideoChannelsView: View {
    @StateObject private var videoPlayer = VideoChannelPlayer.shared

    var body: some View {
        NavigationStack {
            Group {
                if let error = videoPlayer.fetchError {
                    VStack(spacing: 12) {
                        Image(systemName: "exclamationmark.triangle")
                            .font(.largeTitle)
                            .foregroundColor(.red)
                        Text(error)
                            .multilineTextAlignment(.center)
                            .foregroundColor(.secondary)
                            .padding()
                    }
                } else if videoPlayer.availableChannels.isEmpty {
                    Text("No video channels available")
                        .foregroundColor(.secondary)
                } else {
                    VStack(spacing: 0) {
                        if let image = videoPlayer.currentFrameImage {
                            Image(uiImage: image)
                                .resizable()
                                .aspectRatio(contentMode: .fit)
                                .frame(maxHeight: 220)
                                .cornerRadius(12)
                                .padding()
                        }

                        List(videoPlayer.availableChannels) { channel in
                            let isActive = videoPlayer.activeChannel?.id == channel.id
                            Button {
                                if isActive {
                                    videoPlayer.stopChannel()
                                } else {
                                    videoPlayer.startChannel(channel)
                                }
                            } label: {
                                HStack {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(channel.name)
                                            .fontWeight(isActive ? .semibold : .regular)
                                        Text("\(channel.frameCount) frames")
                                            .font(.caption)
                                            .foregroundColor(.secondary)
                                    }
                                    Spacer()
                                    Image(systemName: isActive ? "stop.circle.fill" : "play.circle")
                                        .foregroundColor(isActive ? .red : .accentColor)
                                        .font(.title2)
                                }
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
            .navigationTitle("Video Channels")
            .task {
                await videoPlayer.fetchChannels()
            }
            .refreshable {
                await videoPlayer.fetchChannels()
            }
        }
    }
}
