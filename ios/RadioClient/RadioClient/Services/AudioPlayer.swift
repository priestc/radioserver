import AVFoundation
import Combine
import MediaPlayer
import UIKit

class AudioPlayer: ObservableObject {
    @Published var currentSong: SongItem?
    @Published var queue: [SongItem] = []
    @Published var playHistory: [PlayedSong] = []
    @Published var isPlaying = false
    @Published var currentTime: Double = 0
    @Published var duration: Double = 0

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var syncTimer: Timer?
    private var pendingPlayed: [PlayedSong] = []

    var apiService: APIService?

    init() {
        setupAudioSession()
        setupRemoteCommands()
    }

    private func setupAudioSession() {
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            print("Audio session setup failed: \(error)")
        }
    }

    private func setupRemoteCommands() {
        let center = MPRemoteCommandCenter.shared()

        center.playCommand.addTarget { [weak self] _ in
            self?.play()
            return .success
        }
        center.pauseCommand.addTarget { [weak self] _ in
            self?.pause()
            return .success
        }
        center.togglePlayPauseCommand.addTarget { [weak self] _ in
            self?.togglePlayPause()
            return .success
        }
        center.nextTrackCommand.addTarget { [weak self] _ in
            self?.skipToNext()
            return .success
        }
    }

    func startSyncTimer() {
        syncTimer?.invalidate()
        syncTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { await self?.performSync() }
        }
        Task { await performSync() }
    }

    func stopSyncTimer() {
        syncTimer?.invalidate()
        syncTimer = nil
    }

    private func performSync() async {
        guard let api = apiService, api.isConfigured else { return }

        do {
            let played = pendingPlayed
            let newItems = try await api.sync(played: played, bufferCacheMB: api.bufferCacheMB)
            await MainActor.run { pendingPlayed.removeAll { p in played.contains { $0.id == p.id } } }

            // Add new items to queue (skip already queued)
            let existingIds = Set(await MainActor.run { self.queue.map(\.id) })
            let toAdd = newItems.filter { !existingIds.contains($0.id) }

            if !toAdd.isEmpty {
                await MainActor.run { queue.append(contentsOf: toAdd) }
            }

            // Download songs in background
            for item in newItems {
                if !CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) {
                    _ = try? await api.downloadSong(playlistItemId: item.id, fileExtension: item.fileExtension)
                }
            }

            // Auto-start playback if nothing is playing
            let shouldStart = await MainActor.run { self.currentSong == nil && !self.queue.isEmpty }
            if shouldStart {
                await MainActor.run { self.playNext() }
            }
        } catch {
            print("Sync failed: \(error)")
        }
    }

    func playNext() {
        guard !queue.isEmpty else {
            currentSong = nil
            updateNowPlaying()
            return
        }
        let song = queue.removeFirst()
        playSong(song)
    }

    func playSong(_ song: SongItem) {
        // Stop previous
        player?.pause()
        removeObservers()

        currentSong = song
        let fileURL = CacheManager.shared.fileURL(for: song.id, ext: song.fileExtension)

        guard CacheManager.shared.hasCached(playlistItemId: song.id, ext: song.fileExtension) else {
            // Not downloaded yet, skip to next
            skipToNext()
            return
        }

        let playerItem = AVPlayerItem(url: fileURL)
        player = AVPlayer(playerItem: playerItem)

        // Time observer
        timeObserver = player?.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.5, preferredTimescale: 600),
            queue: .main
        ) { [weak self] time in
            guard let self else { return }
            self.currentTime = time.seconds
            if let dur = self.player?.currentItem?.duration.seconds, dur.isFinite {
                self.duration = dur
            }
            self.updateNowPlaying()
        }

        // End observer
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: playerItem,
            queue: .main
        ) { [weak self] _ in
            self?.songDidFinish()
        }

        player?.play()
        isPlaying = true
        updateNowPlaying()
    }

    private func songDidFinish() {
        guard let song = currentSong else { return }
        let played = PlayedSong(song: song, playedAt: Date())
        playHistory.insert(played, at: 0)
        pendingPlayed.append(played)
        playNext()
    }

    func play() {
        player?.play()
        isPlaying = true
        updateNowPlaying()
    }

    func pause() {
        player?.pause()
        isPlaying = false
        updateNowPlaying()
    }

    func togglePlayPause() {
        if isPlaying { pause() } else { play() }
    }

    func skipToNext() {
        if let song = currentSong {
            let played = PlayedSong(song: song, playedAt: Date())
            playHistory.insert(played, at: 0)
            pendingPlayed.append(played)
        }
        playNext()
    }

    func seek(to fraction: Double) {
        guard duration > 0 else { return }
        let time = CMTime(seconds: fraction * duration, preferredTimescale: 600)
        player?.seek(to: time)
    }

    private func updateNowPlaying() {
        var info = [String: Any]()
        if let song = currentSong {
            info[MPMediaItemPropertyTitle] = song.title
            info[MPMediaItemPropertyArtist] = song.artist
            if let album = song.album {
                info[MPMediaItemPropertyAlbumTitle] = album
            }
            if let dur = song.duration {
                info[MPMediaItemPropertyPlaybackDuration] = dur
            }
            info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = currentTime
            info[MPNowPlayingInfoPropertyPlaybackRate] = isPlaying ? 1.0 : 0.0

            // Load artwork if available
            if let albumId = song.albumId, let api = apiService, let artURL = api.coverArtURL(albumId: albumId) {
                loadArtwork(from: artURL) { image in
                    if let image {
                        var updatedInfo = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
                        let artwork = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
                        updatedInfo[MPMediaItemPropertyArtwork] = artwork
                        MPNowPlayingInfoCenter.default().nowPlayingInfo = updatedInfo
                    }
                }
            }
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func loadArtwork(from url: URL, completion: @escaping (UIImage?) -> Void) {
        var request = URLRequest(url: url)
        if let api = apiService {
            request.setValue("Bearer \(api.apiKey)", forHTTPHeaderField: "Authorization")
        }
        URLSession.shared.dataTask(with: request) { data, _, _ in
            DispatchQueue.main.async {
                if let data, let image = UIImage(data: data) {
                    completion(image)
                } else {
                    completion(nil)
                }
            }
        }.resume()
    }

    private func removeObservers() {
        if let obs = timeObserver {
            player?.removeTimeObserver(obs)
            timeObserver = nil
        }
        if let obs = endObserver {
            NotificationCenter.default.removeObserver(obs)
            endObserver = nil
        }
    }

    deinit {
        removeObservers()
        syncTimer?.invalidate()
    }
}
