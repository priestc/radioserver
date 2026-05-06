import UIKit
import MediaPlayer

class VideoChannelPlayer: ObservableObject {
    static let shared = VideoChannelPlayer()

    @Published var activeChannel: VideoChannel?
    @Published var currentFrameImage: UIImage?
    @Published var availableChannels: [VideoChannel] = []
    @Published var fetchError: String?

    private var currentFrameIndex = 0
    private var frameTimer: Timer?
    private var prefetchedImage: UIImage?
    private var prefetchTask: Task<Void, Never>?

    func fetchChannels() async {
        await MainActor.run { self.fetchError = nil }
        do {
            let channels = try await APIService.shared.fetchVideoChannels()
            await MainActor.run { self.availableChannels = channels }
        } catch {
            await MainActor.run { self.fetchError = error.localizedDescription }
        }
    }

    @MainActor
    func startChannel(_ channel: VideoChannel) {
        guard channel.frameCount > 0 else { return }
        stopChannel()
        activeChannel = channel
        currentFrameIndex = 0
        Task { await self.loadFrame(index: 0) }
        let interval = 1.0 / channel.framesPerSecond
        frameTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.advanceFrame() }
        }
    }

    @MainActor
    func stopChannel() {
        frameTimer?.invalidate()
        frameTimer = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        activeChannel = nil
        currentFrameImage = nil
        prefetchedImage = nil
        AudioPlayer.shared.refreshNowPlaying()
    }

    @MainActor
    private func advanceFrame() {
        guard let channel = activeChannel, channel.frameCount > 0 else { return }
        currentFrameIndex = (currentFrameIndex + 1) % channel.frameCount

        if let prefetched = prefetchedImage {
            currentFrameImage = prefetched
            updateNowPlayingArtwork(prefetched)
            prefetchedImage = nil
        } else {
            Task { await self.loadFrame(index: currentFrameIndex) }
        }

        let nextIndex = (currentFrameIndex + 1) % channel.frameCount
        prefetchTask?.cancel()
        prefetchTask = Task { await self.prefetchFrame(index: nextIndex) }
    }

    private func loadFrame(index: Int) async {
        guard let channel = await MainActor.run(body: { self.activeChannel }),
              let url = APIService.shared.videoFrameURL(channelId: channel.id, frameNumber: index) else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            guard let image = UIImage(data: data) else { return }
            await MainActor.run {
                self.currentFrameImage = image
                self.updateNowPlayingArtwork(image)
            }
        } catch {}
    }

    private func prefetchFrame(index: Int) async {
        guard let channel = await MainActor.run(body: { self.activeChannel }),
              let url = APIService.shared.videoFrameURL(channelId: channel.id, frameNumber: index) else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let image = UIImage(data: data) {
                await MainActor.run { self.prefetchedImage = image }
            }
        } catch {}
    }

    private func updateNowPlayingArtwork(_ image: UIImage) {
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        info[MPMediaItemPropertyArtwork] = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }
}
