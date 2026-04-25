import AVFoundation
import Combine
import MediaPlayer
import Network
import UIKit

class AudioPlayer: ObservableObject {
    static let shared = AudioPlayer()
    @Published var currentSong: SongItem?
    @Published var queue: [SongItem] = []
    @Published var playHistory: [PlayedSong] = []
    @Published var isPlaying = false
    @Published var isFillingCache = false
    @Published var cacheUpdateTick = 0
    @Published var currentTime: Double = 0
    @Published var duration: Double = 0
    @Published var selectedChannel: Channel?
    @Published var availableChannels: [Channel] = []

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var pendingPlayed: [PlayedSong] = []
    var artworkCache: [Int: UIImage] = [:]  // albumId -> image
    private var artworkFailed: Set<Int> = []  // albumIds with no artwork
    private var currentArtwork: MPMediaItemArtwork?

    // Per-channel queues. Key is channel ID (nil = All Music).
    private var backgroundQueues: [Int?: [SongItem]] = [:]

    // Pre-warmed AVPlayers for instant channel switching (like AM/FM tuning).
    // Each background channel keeps a silent, buffered player for its first song.
    private struct PrewarmedChannel {
        let player: AVPlayer
        let item: AVPlayerItem
        let song: SongItem
    }
    private var prewarmedChannels: [Int?: PrewarmedChannel] = [:]

    // Saved playback position (seconds) per channel so switching back resumes mid-song
    private var channelPlaybackPositions: [Int?: Double] = [:]

    // Sync state
    private var hasSyncedCurrentSong = false
    private var currentSongStartedAt: Date?
    private var syncRetryTask: Task<Void, Never>?
    private var syncBackoffSeconds: Double = 2

    // Network monitoring
    private let networkMonitor = NWPathMonitor()
    private(set) var isCellular = false

    var apiService: APIService?

    init() {
        setupAudioSession()
        setupRemoteCommands()
        startNetworkMonitor()
        setupAudioSessionObservers()
    }

    private func startNetworkMonitor() {
        networkMonitor.pathUpdateHandler = { [weak self] path in
            DispatchQueue.main.async {
                self?.isCellular = path.usesInterfaceType(.cellular)
            }
        }
        networkMonitor.start(queue: DispatchQueue.global(qos: .utility))
    }

    private func setupAudioSession() {
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            print("Audio session setup failed: \(error)")
        }
    }

    private func setupAudioSessionObservers() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleRouteChange(_:)),
            name: AVAudioSession.routeChangeNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleInterruption(_:)),
            name: AVAudioSession.interruptionNotification,
            object: nil
        )
    }

    @objc private func handleRouteChange(_ notification: Notification) {
        guard let reasonValue = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
              let reason = AVAudioSession.RouteChangeReason(rawValue: reasonValue) else { return }
        DispatchQueue.main.async {
            switch reason {
            case .newDeviceAvailable:
                // New output (e.g. CarPlay connecting) — resume if we were playing
                if self.isPlaying { self.player?.play() }
            case .oldDeviceUnavailable:
                // Output removed (e.g. headphones unplugged) — pause
                self.pause()
            default:
                break
            }
        }
    }

    @objc private func handleInterruption(_ notification: Notification) {
        guard let typeValue = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: typeValue) else { return }
        if type == .ended {
            let options = AVAudioSession.InterruptionOptions(
                rawValue: notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            )
            if options.contains(.shouldResume) {
                DispatchQueue.main.async { self.play() }
            }
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
        // Perform an initial sync to populate the queue
        Task { await performSync() }
    }

    func fetchChannels() {
        guard let api = apiService else { return }
        Task {
            if let channels = try? await api.fetchChannels() {
                await MainActor.run { self.availableChannels = channels }
                await syncBackgroundChannels()
            }
        }
    }

    func selectChannel(_ channel: Channel?) {
        guard selectedChannel != channel else { return }

        // Save current queue and playback position so we can resume mid-song on return
        var savedQueue = queue
        if let song = currentSong {
            channelPlaybackPositions[selectedChannel?.id] = currentTime
            savedQueue.insert(song, at: 0)
        } else {
            channelPlaybackPositions.removeValue(forKey: selectedChannel?.id)
        }
        backgroundQueues[selectedChannel?.id] = savedQueue

        // Stop current playback
        player?.pause()
        removeObservers()
        isPlaying = false
        hasSyncedCurrentSong = false
        currentSongStartedAt = nil
        currentTime = 0
        duration = 0

        selectedChannel = channel
        queue = backgroundQueues[channel?.id] ?? []

        // Use the pre-warmed player if available — nearly zero silence
        if let prewarmed = prewarmedChannels.removeValue(forKey: channel?.id) {
            queue.removeAll { $0.id == prewarmed.song.id }
            player = prewarmed.player
            currentSong = prewarmed.song
            currentSongStartedAt = Date()
            hasSyncedCurrentSong = false

            attachObservers(to: prewarmed.player, playerItem: prewarmed.item, song: prewarmed.song)
            applyReplayGain(prewarmed.song, to: prewarmed.player)
            prewarmed.player.play()

            // Resume from saved position if returning mid-song
            if let savedTime = channelPlaybackPositions.removeValue(forKey: channel?.id), savedTime > 1 {
                prewarmed.player.seek(to: CMTime(seconds: savedTime, preferredTimescale: 600))
            }

            isPlaying = true
            loadArtworkForCurrentSong()
            updateNowPlaying()
            triggerSync()
        } else {
            currentSong = nil
            updateNowPlaying()
            triggerSync()

            // Fall back: start from cached queue if something is already downloaded
            if !queue.isEmpty {
                let hasCached = queue.contains {
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension) ||
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3")
                }
                if hasCached {
                    playNext()
                    // Resume from saved position if returning mid-song
                    if let savedTime = channelPlaybackPositions.removeValue(forKey: channel?.id), savedTime > 1 {
                        player?.seek(to: CMTime(seconds: savedTime, preferredTimescale: 600))
                    }
                }
            }
        }
    }

    func stopSyncTimer() {
        syncRetryTask?.cancel()
        syncRetryTask = nil
    }

    private func triggerSync() {
        syncRetryTask?.cancel()
        syncBackoffSeconds = 2
        syncRetryTask = Task { await performSyncWithRetry() }
    }

    private func performSyncWithRetry() async {
        while !Task.isCancelled {
            let success = await performSync()
            if success { return }

            // Backoff and retry
            let delay = syncBackoffSeconds
            syncBackoffSeconds = min(syncBackoffSeconds * 2, 60)
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
    }

    private func performSync() async -> Bool {
        guard let api = apiService, api.isConfigured else { return false }

        do {
            let played = pendingPlayed

            // Build now_playing info for the current song
            var nowPlaying: (id: Int, startedAt: Date)?
            if let song = await MainActor.run(body: { self.currentSong }),
               let startedAt = await MainActor.run(body: { self.currentSongStartedAt }) {
                nowPlaying = (id: song.id, startedAt: startedAt)
            }

            let channelId = await MainActor.run { self.selectedChannel?.id }
            let onCellular = await MainActor.run { self.isCellular }
            let isLocal = await MainActor.run { self.apiService?.isOnLocalNetwork ?? false }
            let syncBuffer = (!onCellular && isLocal) ? max(api.bufferCacheMB, 500) : api.bufferCacheMB
            let newItems = try await api.sync(played: played, bufferCacheMB: syncBuffer, nowPlaying: nowPlaying, channelId: channelId)
            await MainActor.run { pendingPlayed.removeAll { p in played.contains { $0.id == p.id } } }

            // Add new items to queue (skip already queued)
            let existingIds = Set(await MainActor.run { self.queue.map(\.id) })
            let toAdd = newItems.filter { !existingIds.contains($0.id) }

            if !toAdd.isEmpty {
                await MainActor.run { queue.append(contentsOf: toAdd) }
            }

            // Download songs and artwork in background

            if onCellular {
                // On cellular: keep at most 2 low-bitrate songs cached.
                let allQueued = await MainActor.run { self.queue }
                let cachedCount = allQueued.filter {
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3") ||
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension)
                }.count
                if cachedCount < 2 {
                    if let next = allQueued.first(where: {
                        !CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3") &&
                        !CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension)
                    }) {
                        _ = try? await api.downloadSong(playlistItemId: next.id, fileExtension: next.fileExtension, lowBitrate: true)
                        if let albumId = next.albumId {
                            await prefetchArtwork(albumId: albumId, api: api)
                        }
                        let idle = await MainActor.run { self.currentSong == nil && !self.queue.isEmpty }
                        if idle { await MainActor.run { self.playNext() } }
                    }
                }
            } else {
                let queued = await MainActor.run { self.queue }
                let newIds = Set(newItems.map { $0.id })
                let allItems = newItems + queued.filter { !newIds.contains($0.id) }
                for item in allItems {
                    if !CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) {
                        _ = try? await api.downloadSong(playlistItemId: item.id, fileExtension: item.fileExtension)
                        let idle = await MainActor.run { self.currentSong == nil && !self.queue.isEmpty }
                        if idle { await MainActor.run { self.playNext() } }
                    }
                    if let albumId = item.albumId {
                        await prefetchArtwork(albumId: albumId, api: api)
                    }
                }
                await MainActor.run { self.cacheUpdateTick += 1 }
            }

            // Final auto-start check: only if a cached file is actually ready
            let shouldStart = await MainActor.run {
                guard self.currentSong == nil, !self.queue.isEmpty else { return false }
                return self.queue.contains {
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension) ||
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3")
                }
            }
            if shouldStart {
                await MainActor.run { self.playNext() }
            }

            // Fire-and-forget: prefill every other channel's queue and pre-warm their players
            Task { await syncBackgroundChannels() }

            return true
        } catch {
            print("Sync failed: \(error)")
            return false
        }
    }

    private func syncBackgroundChannels() async {
        guard let api = apiService, api.isConfigured else { return }
        let activeId = await MainActor.run { selectedChannel?.id }
        var channelIds: [Int?] = [nil]
        channelIds += await MainActor.run { availableChannels.map { Optional($0.id) } }
        for channelId in channelIds where channelId != activeId {
            await prefillBackgroundQueue(channelId: channelId, api: api)
        }
    }

    private func prefillBackgroundQueue(channelId: Int?, api: APIService, bufferMB: Double? = nil, ignoreCellular: Bool = false) async {
        do {
            let existing = await MainActor.run { backgroundQueues[channelId] ?? [] }
            let existingIds = Set(existing.map { $0.id })

            let onCellular = await MainActor.run { isCellular }
            let isLocal = await MainActor.run { self.apiService?.isOnLocalNetwork ?? false }
            let effectiveBuffer: Int
            if let override = bufferMB {
                effectiveBuffer = Int(override)
            } else if !onCellular && isLocal {
                effectiveBuffer = max(api.bufferCacheMB, 500)
            } else {
                effectiveBuffer = api.bufferCacheMB
            }

            let newItems = try await api.sync(
                played: [],
                bufferCacheMB: effectiveBuffer,
                nowPlaying: nil,
                channelId: channelId
            )
            let toAdd = newItems.filter { !existingIds.contains($0.id) }
            if !toAdd.isEmpty {
                await MainActor.run {
                    var q = self.backgroundQueues[channelId] ?? []
                    q.append(contentsOf: toAdd)
                    self.backgroundQueues[channelId] = q
                }
            }

            // Download on WiFi at full quality; skip on cellular unless explicitly requested
            guard !onCellular || ignoreCellular else { return }

            let all = await MainActor.run { backgroundQueues[channelId] ?? [] }
            for item in all {
                if !CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) {
                    _ = try? await api.downloadSong(playlistItemId: item.id, fileExtension: item.fileExtension)
                }
                if let albumId = item.albumId {
                    await prefetchArtwork(albumId: albumId, api: api)
                }
            }
            await MainActor.run { self.cacheUpdateTick += 1 }

            // Pre-warm a silent AVPlayer for the first cached song so channel switching is instant
            let firstCached = await MainActor.run {
                (backgroundQueues[channelId] ?? []).first {
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension) ||
                    CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3")
                }
            }
            if let song = firstCached {
                let ext = CacheManager.shared.hasCached(playlistItemId: song.id, ext: song.fileExtension)
                    ? song.fileExtension : "mp3"
                await MainActor.run { self.prewarmIfNeeded(channelId: channelId, song: song, ext: ext) }
            }
        } catch {
            // Background sync failures are non-fatal
        }
    }

    func fillAllCaches() {
        guard !isFillingCache else { return }
        isFillingCache = true
        Task {
            guard let api = apiService, api.isConfigured else {
                await MainActor.run { self.isFillingCache = false }
                return
            }
            var channelIds: [Int?] = [nil]
            let channels = await MainActor.run { availableChannels }
            channelIds += channels.map { Optional($0.id) }
            for channelId in channelIds {
                await prefillBackgroundQueue(channelId: channelId, api: api, bufferMB: 2000, ignoreCellular: true)
            }
            await MainActor.run {
                self.isFillingCache = false
                self.cacheUpdateTick += 1
            }
        }
    }

    /// Creates a silent, buffered AVPlayer for a background channel so that switching to it is near-instant.
    private func prewarmIfNeeded(channelId: Int?, song: SongItem, ext: String) {
        // Don't replace an existing pre-warmed player
        guard prewarmedChannels[channelId] == nil else { return }
        let url = CacheManager.shared.fileURL(for: song.id, ext: ext)
        let item = AVPlayerItem(url: url)
        let p = AVPlayer(playerItem: item)
        p.volume = 0  // Silent until activated on channel switch
        prewarmedChannels[channelId] = PrewarmedChannel(player: p, item: item, song: song)
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
        player?.pause()
        removeObservers()

        currentSong = song
        hasSyncedCurrentSong = false
        currentSongStartedAt = Date()

        // Check for both original and low-bitrate cached versions
        let ext: String
        if CacheManager.shared.hasCached(playlistItemId: song.id, ext: song.fileExtension) {
            ext = song.fileExtension
        } else if CacheManager.shared.hasCached(playlistItemId: song.id, ext: "mp3") {
            ext = "mp3"
        } else {
            // Not cached yet — put back at front of queue; download loop will play it
            queue.insert(song, at: 0)
            currentSong = nil
            return
        }
        let fileURL = CacheManager.shared.fileURL(for: song.id, ext: ext)

        let playerItem = AVPlayerItem(url: fileURL)
        let avPlayer = AVPlayer(playerItem: playerItem)
        player = avPlayer

        attachObservers(to: avPlayer, playerItem: playerItem, song: song)
        applyReplayGain(song, to: avPlayer)

        avPlayer.play()
        isPlaying = true
        loadArtworkForCurrentSong()
        updateNowPlaying()
    }

    /// Attaches time and end-of-track observers to an AVPlayer instance.
    private func attachObservers(to avPlayer: AVPlayer, playerItem: AVPlayerItem, song: SongItem) {
        timeObserver = avPlayer.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.5, preferredTimescale: 600),
            queue: .main
        ) { [weak self] time in
            guard let self else { return }
            self.currentTime = time.seconds
            if let dur = avPlayer.currentItem?.duration.seconds, dur.isFinite {
                self.duration = dur

                // Trigger sync at 50%
                if !self.hasSyncedCurrentSong && time.seconds >= dur / 2 {
                    self.hasSyncedCurrentSong = true
                    if let current = self.currentSong {
                        let played = PlayedSong(song: current, playedAt: Date(), skipped: false)
                        self.pendingPlayed.append(played)
                    }
                    self.triggerSync()
                }
            }
            self.updateNowPlaying()
        }

        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: playerItem,
            queue: .main
        ) { [weak self] _ in
            self?.songDidFinish()
        }
    }

    private func applyReplayGain(_ song: SongItem, to avPlayer: AVPlayer) {
        if let gainDB = song.replaygainTrackGain {
            avPlayer.volume = Float(min(pow(10.0, gainDB / 20.0), 1.0))
        } else {
            avPlayer.volume = 1.0
        }
    }

    private func songDidFinish() {
        guard let song = currentSong else { return }
        if !hasSyncedCurrentSong {
            let played = PlayedSong(song: song, playedAt: Date(), skipped: false)
            pendingPlayed.append(played)
        }
        playHistory.insert(PlayedSong(song: song, playedAt: Date(), skipped: false), at: 0)
        removeCachedFiles(for: song)
        playNext()
        triggerSync()
    }

    private func removeCachedFiles(for song: SongItem) {
        CacheManager.shared.removeFile(for: song.id, ext: song.fileExtension)
        if song.fileExtension != "mp3" {
            CacheManager.shared.removeFile(for: song.id, ext: "mp3")
        }
    }

    func play() {
        if currentSong == nil {
            if queue.isEmpty {
                triggerSync()
            } else {
                playNext()
            }
            return
        }
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
            let played = PlayedSong(song: song, playedAt: Date(), skipped: true)
            if !hasSyncedCurrentSong {
                pendingPlayed.append(played)
            }
            playHistory.insert(played, at: 0)
            removeCachedFiles(for: song)
        }
        playNext()
        triggerSync()
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
            if let year = song.year {
                info[MPMediaItemPropertyAlbumTrackNumber] = year
            }
            info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = currentTime
            info[MPNowPlayingInfoPropertyPlaybackRate] = isPlaying ? 1.0 : 0.0
            if let artwork = currentArtwork {
                info[MPMediaItemPropertyArtwork] = artwork
            }
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    func loadArtworkForCurrentSong() {
        guard let song = currentSong, let albumId = song.albumId else {
            currentArtwork = nil
            return
        }

        if let cached = artworkCache[albumId] {
            currentArtwork = MPMediaItemArtwork(boundsSize: cached.size) { _ in cached }
        } else {
            currentArtwork = nil
        }
        updateNowPlaying()
    }

    private func prefetchArtwork(albumId: Int, api: APIService) async {
        let skip = await MainActor.run { self.artworkCache[albumId] != nil || self.artworkFailed.contains(albumId) }
        if skip { return }

        if let diskImage = CacheManager.shared.cachedArtwork(for: albumId) {
            await MainActor.run { self.artworkCache[albumId] = diskImage }
            return
        }

        guard let artURL = api.coverArtURL(albumId: albumId) else { return }

        do {
            let (data, response) = try await URLSession.shared.data(from: artURL)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 404 {
                await MainActor.run { self.artworkFailed.insert(albumId) }
                return
            }
            if let image = UIImage(data: data) {
                CacheManager.shared.saveArtwork(image, for: albumId)
                await MainActor.run { self.artworkCache[albumId] = image }
            } else {
                await MainActor.run { self.artworkFailed.insert(albumId) }
            }
        } catch {
            // Network error — don't mark as failed so it can retry next sync
        }
    }

    /// Returns audio cache size in MB for every channel (including ones not yet background-synced).
    func cacheSizeMBPerChannel() -> [(name: String, sizeMB: Double)] {
        // Always enumerate all known channels so every row is visible even at 0 MB
        var entries: [(name: String, channelId: Int?)] = [("All Music", nil)]
        for ch in availableChannels {
            entries.append((ch.name, Optional(ch.id)))
        }

        return entries.map { (name, channelId) in
            let items: [SongItem]
            if channelId == selectedChannel?.id {
                // Active channel: current song + remaining queue
                var active = queue
                if let song = currentSong { active.insert(song, at: 0) }
                items = active
            } else {
                items = backgroundQueues[channelId] ?? []
            }
            let size = items.reduce(0.0) { total, item in
                total
                    + CacheManager.shared.fileSizeMB(for: item.id, ext: item.fileExtension)
                    + (item.fileExtension != "mp3"
                        ? CacheManager.shared.fileSizeMB(for: item.id, ext: "mp3")
                        : 0)
            }
            return (name: name, sizeMB: size)
        }
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
        syncRetryTask?.cancel()
        networkMonitor.cancel()
    }
}
