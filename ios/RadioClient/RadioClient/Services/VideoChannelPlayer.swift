import UIKit
import MediaPlayer
import AVFoundation

class VideoChannelPlayer: ObservableObject {
    static let shared = VideoChannelPlayer()

    @Published var activeChannel: VideoChannel?
    @Published var currentFrameImage: UIImage?
    @Published var availableChannels: [VideoChannel] = []
    @Published var fetchError: String?
    @Published var frameStep: Int = 1

    private var currentFrameIndex = 0
    private var frameTimer: Timer?
    private var prefetchedImage: UIImage?
    private var prefetchTask: Task<Void, Never>?
    private var audioPlayer: AVPlayer?
    private var wasRadioPlaying = false

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

        wasRadioPlaying = AudioPlayer.shared.isPlaying
        if wasRadioPlaying { AudioPlayer.shared.pause() }

        activeChannel = channel
        currentFrameIndex = 0
        frameStep = 1

        if let audioURL = APIService.shared.videoAudioURL(channelId: channel.id) {
            audioPlayer = AVPlayer(url: audioURL)
            audioPlayer?.play()
        }

        updateNowPlayingChannelInfo()
        Task { await self.loadFrame(index: 0) }
        frameTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.advanceFrame() }
        }
    }

    @MainActor
    func stopChannel() {
        frameTimer?.invalidate()
        frameTimer = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        audioPlayer?.pause()
        audioPlayer = nil
        activeChannel = nil
        currentFrameImage = nil
        prefetchedImage = nil
        frameStep = 1

        if wasRadioPlaying {
            AudioPlayer.shared.play()
            wasRadioPlaying = false
        }
        AudioPlayer.shared.refreshNowPlaying()
    }

    @MainActor
    func increaseFrameStep() {
        frameStep = min(frameStep + 1, 60)
        updateNowPlayingStepInfo()
    }

    @MainActor
    func decreaseFrameStep() {
        frameStep = max(1, frameStep - 1)
        updateNowPlayingStepInfo()
    }

    @MainActor
    private func advanceFrame() {
        guard let channel = activeChannel, channel.frameCount > 0 else { return }
        currentFrameIndex = (currentFrameIndex + frameStep) % channel.frameCount

        if let prefetched = prefetchedImage {
            currentFrameImage = prefetched
            updateNowPlayingArtwork(prefetched)
            prefetchedImage = nil
        } else {
            Task { await self.loadFrame(index: currentFrameIndex) }
        }

        let nextIndex = (currentFrameIndex + frameStep) % channel.frameCount
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

    private func updateNowPlayingChannelInfo() {
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        info[MPMediaItemPropertyTitle] = activeChannel?.name ?? "Video"
        info[MPMediaItemPropertyArtist] = "Step: \(frameStep)x"
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func updateNowPlayingStepInfo() {
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        info[MPMediaItemPropertyArtist] = "Step: \(frameStep)x"
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }
}
