import UIKit
import MediaPlayer
import AVFoundation

class VideoChannelPlayer: ObservableObject {
    static let shared = VideoChannelPlayer()

    @Published var activeChannel: VideoChannel?
    @Published var currentFrameImage: UIImage?
    @Published var availableChannels: [VideoChannel] = []
    @Published var fetchError: String?
    @Published var displayFps: Int = 1
    @Published var isBuffering = false

    private var currentFrameIndex = 0
    private var frameTimer: Timer?
    private var prefetchTask: Task<Void, Never>?
    private var frameCache: [Int: UIImage] = [:]
    private var audioPlayer: AVPlayer?
    private var wasRadioPlaying = false

    private let preBufferTicks = 10

    // Frames to skip per timer tick so video always advances at real-time speed.
    private var computedFrameStep: Int {
        guard let channel = activeChannel else { return 1 }
        return max(1, Int((channel.nativeFps / Double(displayFps)).rounded()))
    }

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
        print("[VideoPlayer] startChannel: \(channel.name)")
        stopChannel()

        wasRadioPlaying = AudioPlayer.shared.isPlaying
        print("[VideoPlayer] wasRadioPlaying: \(wasRadioPlaying)")
        if wasRadioPlaying { AudioPlayer.shared.pause() }

        activeChannel = channel
        currentFrameIndex = 0
        displayFps = 1
        frameCache = [:]
        isBuffering = true

        updateNowPlayingInfo()

        Task {
            await self.fillBuffer(channel: channel, from: 0)
            await MainActor.run { self.beginPlayback(channel: channel) }
        }
    }

    @MainActor
    func stopChannel() {
        print("[VideoPlayer] stopChannel, wasRadioPlaying: \(wasRadioPlaying)")
        frameTimer?.invalidate()
        frameTimer = nil
        prefetchTask?.cancel()
        prefetchTask = nil
        audioPlayer?.pause()
        audioPlayer = nil
        activeChannel = nil
        currentFrameImage = nil
        frameCache = [:]
        displayFps = 1
        isBuffering = false

        if wasRadioPlaying {
            print("[VideoPlayer] restoring audio session and resuming radio")
            try? AVAudioSession.sharedInstance().setActive(true)
            AudioPlayer.shared.play()
            wasRadioPlaying = false
        }
        AudioPlayer.shared.refreshNowPlaying()
    }

    @MainActor
    func increaseDisplayFps() {
        guard let channel = activeChannel else { return }
        guard let next = validFpsValues(for: channel).first(where: { $0 > displayFps }) else { return }
        displayFps = next
        restartTimer()
        updateNowPlayingInfo()
    }

    @MainActor
    func decreaseDisplayFps() {
        guard let channel = activeChannel else { return }
        guard let prev = validFpsValues(for: channel).last(where: { $0 < displayFps }) else { return }
        displayFps = prev
        restartTimer()
        updateNowPlayingInfo()
    }

    private func validFpsValues(for channel: VideoChannel) -> [Int] {
        let n = max(1, Int(channel.nativeFps.rounded()))
        return (1...n).filter { n % $0 == 0 }
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

        if let audioURL = APIService.shared.videoAudioURL(channelId: channel.id) {
            audioPlayer = AVPlayer(url: audioURL)
            audioPlayer?.play()
        }

        startTimer()
    }

    @MainActor
    private func startTimer() {
        frameTimer?.invalidate()
        frameTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / Double(displayFps), repeats: true) { [weak self] _ in
            Task { @MainActor in self?.advanceFrame() }
        }
    }

    @MainActor
    private func restartTimer() {
        frameCache = [:]
        prefetchTask?.cancel()
        startTimer()
        guard let channel = activeChannel else { return }
        let from = currentFrameIndex
        prefetchTask = Task { await self.fillBuffer(channel: channel, from: from) }
    }

    @MainActor
    private func advanceFrame() {
        guard let channel = activeChannel, channel.frameCount > 0 else { return }
        let step = computedFrameStep
        currentFrameIndex = (currentFrameIndex + step) % channel.frameCount

        if let image = frameCache[currentFrameIndex] {
            currentFrameImage = image
            updateNowPlayingArtwork(image)
            frameCache.removeValue(forKey: currentFrameIndex)
        }

        // Refill lookahead window
        let cid = channel.id
        let fc = channel.frameCount
        for i in 1...preBufferTicks {
            let idx = (currentFrameIndex + i * step) % fc
            if frameCache[idx] == nil {
                Task { await self.fetchIntoCache(index: idx, channelId: cid) }
            }
        }
    }

    private func fillBuffer(channel: VideoChannel, from startIndex: Int) async {
        let step = await MainActor.run { self.computedFrameStep }
        await withTaskGroup(of: (Int, UIImage?).self) { group in
            for i in 0..<preBufferTicks {
                let idx = (startIndex + i * step) % channel.frameCount
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

    private func updateNowPlayingInfo() {
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        info[MPMediaItemPropertyTitle] = activeChannel?.name ?? "Video"
        info[MPMediaItemPropertyArtist] = "\(displayFps) fps"
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }
}
