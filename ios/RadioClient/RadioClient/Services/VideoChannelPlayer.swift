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
    @Published var isBuffering = false

    private var currentFrameIndex = 0
    private var frameTimer: Timer?
    private var prefetchTask: Task<Void, Never>?
    private var frameCache: [Int: UIImage] = [:]
    private var audioPlayer: AVPlayer?
    private var wasRadioPlaying = false

    private let preBufferCount = 10

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
        frameCache = [:]
        isBuffering = true

        updateNowPlayingChannelInfo()

        Task {
            await self.fillBuffer(channel: channel, from: 0)
            await MainActor.run { self.beginPlayback(channel: channel) }
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
        frameCache = [:]
        frameStep = 1
        isBuffering = false

        if wasRadioPlaying {
            AudioPlayer.shared.play()
            wasRadioPlaying = false
        }
        AudioPlayer.shared.refreshNowPlaying()
    }

    @MainActor
    func increaseFrameStep() {
        frameStep = min(frameStep + 1, 60)
        rebuffer()
        updateNowPlayingStepInfo()
    }

    @MainActor
    func decreaseFrameStep() {
        frameStep = max(1, frameStep - 1)
        rebuffer()
        updateNowPlayingStepInfo()
    }

    // MARK: - Private

    @MainActor
    private func beginPlayback(channel: VideoChannel) {
        guard activeChannel?.id == channel.id else { return }
        isBuffering = false

        if let image = frameCache[0] {
            currentFrameImage = image
            updateNowPlayingArtwork(image)
        }

        // Audio starts in sync with first frame
        if let audioURL = APIService.shared.videoAudioURL(channelId: channel.id) {
            audioPlayer = AVPlayer(url: audioURL)
            audioPlayer?.play()
        }

        frameTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.advanceFrame() }
        }
    }

    @MainActor
    private func advanceFrame() {
        guard let channel = activeChannel, channel.frameCount > 0 else { return }
        currentFrameIndex = (currentFrameIndex + frameStep) % channel.frameCount

        if let image = frameCache[currentFrameIndex] {
            currentFrameImage = image
            updateNowPlayingArtwork(image)
            frameCache.removeValue(forKey: currentFrameIndex)
        }

        // Replenish the cache one window ahead
        let fetchIdx = (currentFrameIndex + preBufferCount * frameStep) % channel.frameCount
        let cid = channel.id
        Task { await self.fetchIntoCache(index: fetchIdx, channelId: cid) }
    }

    @MainActor
    private func rebuffer() {
        frameCache = [:]
        prefetchTask?.cancel()
        guard let channel = activeChannel else { return }
        let from = currentFrameIndex
        prefetchTask = Task { await self.fillBuffer(channel: channel, from: from) }
    }

    private func fillBuffer(channel: VideoChannel, from startIndex: Int) async {
        await withTaskGroup(of: (Int, UIImage?).self) { group in
            for i in 0..<preBufferCount {
                let idx = (startIndex + i * (await MainActor.run { self.frameStep })) % channel.frameCount
                group.addTask { [self] in
                    await (idx, self.fetchImage(channelId: channel.id, index: idx))
                }
            }
            for await (idx, image) in group {
                if let image = image {
                    await MainActor.run { self.frameCache[idx] = image }
                }
            }
        }
    }

    private func fetchIntoCache(index: Int, channelId: Int) async {
        if await MainActor.run(body: { self.frameCache[index] != nil }) { return }
        if let image = await fetchImage(channelId: channelId, index: index) {
            await MainActor.run { self.frameCache[index] = image }
        }
    }

    private func fetchImage(channelId: Int, index: Int) async -> UIImage? {
        guard let url = APIService.shared.videoFrameURL(channelId: channelId, frameNumber: index) else { return nil }
        guard let (data, _) = try? await URLSession.shared.data(from: url) else { return nil }
        return UIImage(data: data)
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
