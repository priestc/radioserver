import AVFoundation
import Combine
import MediaPlayer
import Network
import UIKit

class AudioPlayer: ObservableObject {
    static let shared = AudioPlayer()
    @Published var currentSong: SongItem?
    @Published var queue: [SongItem] = []
    @Published var isPlaying = false
    @Published var isFillingCache = false
    @Published var cacheUpdateTick = 0
    @Published var currentTime: Double = 0
    @Published var duration: Double = 0
    @Published var selectedChannel: Channel?
    @Published var availableChannels: [Channel] = []
    @Published var exhaustedChannelIds: Set<Int?> = []
    /// Cover art for the current song, loaded from the on-disk artwork cache (or nil if
    /// none is cached). Views read this instead of an in-memory dictionary so artwork
    /// shows on a fully offline launch, when nothing has been fetched this session.
    @Published private(set) var currentArtworkImage: UIImage?

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var failedObserver: NSObjectProtocol?
    private var statusObserver: NSKeyValueObservation?
    private var pendingPlayed: [PlayedSong] = []
    private var artworkFailed: Set<Int> = []  // albumIds the server has no artwork for
    private var currentArtwork: MPMediaItemArtwork?

    /// Small in-memory front for the on-disk artwork cache. The previous unbounded
    /// `[Int: UIImage]` grew by one image for every album prefetched in every channel,
    /// which on a large library is enough to get the app jetsammed in the background —
    /// and a killed app is silence in the car. NSCache evicts under memory pressure.
    private let artworkMemoryCache: NSCache<NSNumber, UIImage> = {
        let cache = NSCache<NSNumber, UIImage>()
        cache.countLimit = 40
        return cache
    }()

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
    private static let maxPrewarmedChannels = 8

    // Saved playback position (seconds) per channel so switching back resumes mid-song
    private var channelPlaybackPositions: [Int?: Double] = [:]

    // Sync state
    private var hasSyncedCurrentSong = false
    private var hasCheckedCurrentSongForCorruption = false
    private var currentSongStartedAt: Date?
    private var syncRetryTask: Task<Void, Never>?
    private var configChangeTask: Task<Void, Never>?
    private var isBackgroundSyncing = false
    private var loggedNotConfigured = false

    /// Runs while playback is idle with nothing playable on the current channel, and
    /// moves to a channel that does have downloaded music if nothing arrives in time.
    private var idleFallbackTask: Task<Void, Never>?
    private static let idleFallbackGraceSeconds: UInt64 = 20

    /// How many times the current file failed to play (keyed by song id). One retry is
    /// allowed — AVPlayer items do occasionally fail after a long background stint for
    /// reasons unrelated to the file — after which the file is treated as corrupt.
    private var playbackFailureCounts: [Int: Int] = [:]

    /// Stop hammering an unreachable server: after this many back-to-back download
    /// failures in one pass, the rest of the pass is skipped until the next sync.
    private static let maxConsecutiveDownloadFailures = 3

    // Channels currently being downloaded into. Guards against overlapping download
    // loops (active-channel sync, background prefill, Fill All Caches) racing past a
    // channel's configured cache limit — each checks the limit against live disk state,
    // so without this, two loops can both see "under limit" and both start downloading.
    private var downloadingChannelIds: Set<Int?> = []

    // Network monitoring
    private let networkMonitor = NWPathMonitor()
    private(set) var isCellular = false
    private var wasNetworkConnected = false

    var apiService: APIService?

    init() {
        setupAudioSession()
        setupRemoteCommands()
        startNetworkMonitor()
        setupAudioSessionObservers()
        loadPendingPlayed()
        // Everything the UI needs is restored from disk before any network call:
        // the channel list, the channel that was playing, the per-channel cache
        // numbers, and (below) the song metadata for every cached audio file.
        loadPersistedChannels()
        loadPersistedCacheStats()
        AppLogger.shared.log(.startup, "App started")
        // CRITICAL, NEVER REMOVE: this app must be able to play music with zero
        // network connectivity, using only what's already downloaded to disk. See the
        // "offline playback is non-negotiable" note above performSync() for the full
        // rule. This call is what makes that possible at launch — it must run
        // synchronously here, before any network call is attempted, not after one
        // fails or times out.
        resumeFromDiskCacheIfNeeded()
        // Real recompute (off the main thread) now that the in-memory queues exist.
        recalculateCacheStats()
    }

    /// Reconstructs the playback queue from whatever song metadata + cached audio
    /// survived from the previous session, and starts playback immediately if anything
    /// is playable — all before a single network request has been made. Prefers the
    /// channel that was playing last time; if that one has nothing downloaded, any
    /// channel with downloaded music is used instead, because the only acceptable
    /// reason for silence is an empty cache on every channel (CLAUDE.md).
    private func resumeFromDiskCacheIfNeeded() {
        loadPersistedSongLibrary()
        var target = selectedChannel
        if !hasPlayableSongs(channelId: target?.id),
           let alt = channelWithPlayableSongs(excluding: [target?.id]) {
            AppLogger.shared.log(.startup, "\(channelLabel(for: target?.id)) has nothing downloaded — starting on \(channelLabel(for: alt?.id)) instead")
            target = alt
        }
        selectedChannel = target
        persistSelectedChannel()
        queue = backgroundQueues[target?.id] ?? []
        guard currentSong == nil else { return }
        if playNext() {
            AppLogger.shared.log(.trackPlayed, "Resuming playback from cache while syncing with server")
        } else {
            AppLogger.shared.log(.playbackError, "Nothing downloaded on any channel yet — music starts as soon as the first song downloads")
        }
    }

    private func startNetworkMonitor() {
        networkMonitor.pathUpdateHandler = { [weak self] path in
            let isConnected = path.status == .satisfied
            DispatchQueue.main.async {
                guard let self else { return }
                self.isCellular = path.usesInterfaceType(.cellular)
                if isConnected && !self.wasNetworkConnected {
                    // Network just came back — flush pendingPlayed immediately
                    self.triggerSync(reason: "network reconnected")
                } else if !isConnected && self.wasNetworkConnected {
                    AppLogger.shared.log(.playbackError, "Lost internet connection — downloads paused until it's back; cached music keeps playing")
                }
                self.wasNetworkConnected = isConnected
            }
        }
        networkMonitor.start(queue: DispatchQueue.global(qos: .utility))
    }

    private func setupAudioSession() {
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            AppLogger.shared.log(.playbackError, "Couldn't start audio playback", details: error.localizedDescription)
        }
    }

    func reactivateAudioSession() {
        setupAudioSession()
        // If the player item failed while in the background, recreate it now so
        // the user doesn't have to press play on a broken instance.
        if let song = currentSong,
           let item = player?.currentItem,
           item.status == .failed {
            AppLogger.shared.log(.playbackError, "Fixed playback of \"\(song.title)\" after returning to the app")
            playSong(song)
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
                // New output (e.g. CarPlay/Bluetooth connecting) — resume if we were playing
                if self.isPlaying {
                    AppLogger.shared.log(.playbackError, "Audio device connected — resumed playback")
                    self.player?.play()
                }
            case .oldDeviceUnavailable:
                // Output removed (e.g. headphones/CarPlay/Bluetooth disconnected) — pause.
                // This does not auto-resume, so a flaky Bluetooth/CarPlay connection can
                // leave playback silently paused until the user taps play again.
                AppLogger.shared.log(.playbackError, "Audio device disconnected — paused (tap play to resume)")
                self.pause()
            default:
                break
            }
        }
    }

    @objc private func handleInterruption(_ notification: Notification) {
        guard let typeValue = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: typeValue) else { return }
        switch type {
        case .began:
            AppLogger.shared.log(.playbackError, "Playback paused by the system (e.g. a call or Siri)")
            // The system already paused the player; mirror that so the play/pause
            // button doesn't need two taps to resume.
            DispatchQueue.main.async {
                self.isPlaying = false
                self.updateNowPlaying()
            }
        case .ended:
            let options = AVAudioSession.InterruptionOptions(
                rawValue: notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            )
            if options.contains(.shouldResume) {
                AppLogger.shared.log(.playbackError, "Resumed playback automatically")
                DispatchQueue.main.async { self.play() }
            } else {
                AppLogger.shared.log(.playbackError, "Playback paused — tap play to resume")
            }
        @unknown default:
            break
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
            self?.selectNextChannel()
            return .success
        }
        center.previousTrackCommand.addTarget { [weak self] _ in
            self?.selectPreviousChannel()
            return .success
        }
    }

    func startSyncTimer() {
        triggerSync(reason: "CarPlay connected")
    }

    /// Set when a channel fetch fails, so views can show it — nil while loading or once
    /// a fetch has succeeded. The persisted channel list keeps rendering underneath it.
    @Published var channelsError: String?

    /// The single owner of "/channels/" fetches. Views (e.g. ChannelsView) should call
    /// this rather than hitting APIService directly — otherwise the app-launch fetch
    /// here and a view's own fetch can fire at the same moment, doubling the request.
    func fetchChannels() {
        guard let api = apiService else { return }
        guard api.isConfigured else {
            // Nothing to fetch against; the persisted list (if any) stays on screen.
            if availableChannels.isEmpty {
                channelsError = "Server isn't set up yet — add it in Settings"
            }
            return
        }
        Task {
            do {
                let channels = try await api.fetchChannels()
                await MainActor.run {
                    self.applyFetchedChannels(channels)
                    self.channelsError = nil
                }
                recalculateCacheStats()
                // Do NOT call syncBackgroundChannels() here — background syncs
                // must only run after an active sync has sent pendingPlayed,
                // otherwise the server returns already-played songs for background channels.
            } catch {
                if !error.isCancellation {
                    await MainActor.run { self.channelsError = error.localizedDescription }
                }
            }
        }
    }

    /// Stores a fresh channel list and re-points `selectedChannel` at the matching
    /// fresh record (a renamed channel would otherwise stop comparing equal and lose
    /// its checkmark in the list).
    private func applyFetchedChannels(_ channels: [Channel]) {
        availableChannels = channels
        persistChannels()
        if let current = selectedChannel,
           let fresh = channels.first(where: { $0.id == current.id }),
           fresh != current {
            selectedChannel = fresh
            persistSelectedChannel()
        }
    }

    /// Called when the server URL or API key changes so the app picks the new server
    /// up right away instead of waiting for the next tab switch or foreground.
    func configurationChanged() {
        configChangeTask?.cancel()
        configChangeTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            guard !Task.isCancelled, let api = apiService, api.isConfigured else { return }
            loggedNotConfigured = false
            fetchChannels()
            triggerSync(reason: "server settings changed")
        }
    }

    /// The channels next/previous cycle through. Channels with downloaded music come
    /// first; channels with nothing cached are only included when the network is up
    /// (they'd start downloading on selection) — offline, tuning into one would just
    /// mean silence.
    private func channelCycle() -> [Channel?] {
        let all: [Channel?] = ([nil] + availableChannels).filter { !exhaustedChannelIds.contains($0?.id) }
        let withMusic = all.filter { $0 == selectedChannel || hasPlayableSongs(channelId: $0?.id) }
        return (withMusic.count > 1 || !wasNetworkConnected) ? withMusic : all
    }

    func selectNextChannel() {
        let available = channelCycle()
        guard !available.isEmpty else { return }
        let currentIndex = available.firstIndex(where: { $0 == selectedChannel }) ?? -1
        let nextIndex = (currentIndex + 1) % available.count
        selectChannel(available[nextIndex])
    }

    func selectPreviousChannel() {
        let available = channelCycle()
        guard !available.isEmpty else { return }
        let currentIndex = available.firstIndex(where: { $0 == selectedChannel }) ?? available.count
        let prevIndex = (currentIndex - 1 + available.count) % available.count
        selectChannel(available[prevIndex])
    }

    func selectChannel(_ channel: Channel?) {
        selectChannel(channel, userInitiated: true)
    }

    private func selectChannel(_ channel: Channel?, userInitiated: Bool) {
        guard !exhaustedChannelIds.contains(channel?.id) else { return }
        guard selectedChannel != channel else { return }
        if userInitiated {
            // The user made a choice — don't let a pending automatic fallback override it.
            idleFallbackTask?.cancel()
            idleFallbackTask = nil
        }

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
        hasCheckedCurrentSongForCorruption = false
        currentSongStartedAt = nil
        currentTime = 0
        duration = 0

        selectedChannel = channel
        persistSelectedChannel()
        queue = backgroundQueues[channel?.id] ?? []

        // Use the pre-warmed player if available — nearly zero silence
        if let prewarmed = prewarmedChannels.removeValue(forKey: channel?.id),
           CacheManager.shared.isCached(prewarmed.song) {
            queue.removeAll { $0.id == prewarmed.song.id }
            player = prewarmed.player
            currentSong = prewarmed.song
            currentSongStartedAt = Date()
            hasSyncedCurrentSong = false
            hasCheckedCurrentSongForCorruption = false

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
            triggerSync(reason: "channel switch")
        } else {
            currentSong = nil
            updateNowPlaying()
            triggerSync(reason: "channel switch")

            // Start from the cached queue if anything for this channel is on disk
            if playNext() {
                // Resume from saved position if returning mid-song
                if let savedTime = channelPlaybackPositions.removeValue(forKey: channel?.id), savedTime > 1 {
                    player?.seek(to: CMTime(seconds: savedTime, preferredTimescale: 600))
                }
            } else {
                let name = channelLabel(for: channel?.id)
                if wasNetworkConnected {
                    AppLogger.shared.log(.playbackError, "Nothing downloaded yet for \(name) — it starts as soon as the first song downloads")
                } else {
                    AppLogger.shared.log(.playbackError, "Nothing downloaded for \(name) and there's no connection — pick a channel with downloaded songs")
                }
            }
        }
    }

    func stopSyncTimer() {
        syncRetryTask?.cancel()
        syncRetryTask = nil
    }

    func triggerSync(reason: String) {
        syncRetryTask?.cancel()
        syncRetryTask = Task {
            // Debounce: several triggers often fire within the same instant (e.g. app
            // launch fires both an onAppear sync and a scenePhase-driven one; opening a
            // tab fires its own). Without this, each one starts a real network request
            // only to have the next one cancel it moments later — wasted requests and
            // pointless "cancelled" noise. Waiting briefly lets only the last one in a
            // burst actually run.
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            await performSyncWithRetry(reason: reason)
        }
    }

    private enum SyncOutcome {
        case success
        case failure
        case notConfigured
    }

    private func performSyncWithRetry(reason: String) async {
        var backoff = 2.0
        var attempt = 0
        while !Task.isCancelled {
            attempt += 1
            // Only the first attempt logs its own failure; later ones are summarised
            // periodically so a long stretch offline doesn't fill the log with one
            // identical line per minute.
            let outcome = await performSync(reason: reason, silent: attempt > 1)
            switch outcome {
            case .success, .notConfigured:
                return
            case .failure:
                break
            }
            if attempt > 1 && attempt % 10 == 0 {
                AppLogger.shared.log(.apiFailure, "Still can't reach the server after \(attempt) attempts — will keep trying; cached music keeps playing", details: "trigger: \(reason)")
            }
            try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
            backoff = min(backoff * 2, 60)
        }
    }

    /// Human-readable name for a channel, for use in sync log messages.
    private func channelLabel(for channelId: Int?) -> String {
        guard let channelId else { return "All Music" }
        return availableChannels.first(where: { $0.id == channelId })?.name ?? "channel #\(channelId)"
    }

    private func syncLogReason(_ trigger: String, channelId: Int?) -> String {
        "\(trigger) — \(channelLabel(for: channelId))"
    }

    /// True if `error` is the result of the hosting Task being cancelled (e.g. a newer
    /// triggerSync() superseded this one) rather than a genuine network/server failure.
    /// Once a Task is cancelled every subsequent await on it fails the same way, so callers
    /// should stop retrying further items instead of looping through them one by one.
    private func isCancellation(_ error: Error) -> Bool {
        error.isCancellation
    }

    // CRITICAL, NEVER REMOVE OR WEAKEN THIS INVARIANT: a failed or timed-out sync must
    // NEVER prevent, pause, or delay music that is already cached on disk from playing.
    // This app exists to keep playing music with zero internet connectivity (e.g. a
    // driveway, a basement, a dead zone) — that is the entire reason the caching system
    // exists. performSync() below is allowed to fail, log the failure, and retry later
    // (see performSyncWithRetry's backoff) — it must NEVER be made a precondition for
    // calling playNext()/playSong() elsewhere in this file. The ONLY acceptable reason
    // for music not to be playing is that the on-disk cache has zero playable songs for
    // every channel. If you are tempted to gate playback on `await performSync(...)`
    // succeeding first, don't — see resumeFromDiskCacheIfNeeded() for how launch already
    // avoids this trap.
    private func performSync(reason: String, silent: Bool = false) async -> SyncOutcome {
        let (api, configured) = await MainActor.run { (self.apiService, self.apiService?.isConfigured ?? false) }
        guard let api, configured else {
            // Logged once, not on every retry: without a server there is nothing to
            // retry against. configurationChanged() re-triggers when settings change.
            let alreadyLogged = await MainActor.run { () -> Bool in
                if self.loggedNotConfigured { return true }
                self.loggedNotConfigured = true
                return false
            }
            if !alreadyLogged {
                AppLogger.shared.log(.playbackError, "Can't sync — server isn't set up (add it in Settings). Cached music still plays.")
            }
            return .notConfigured
        }

        // The channel whose download lock this call currently holds (if any), so the
        // error path below releases exactly that and never a lock held by another loop.
        var heldLock: Int?? = nil
        do {
            // Every read of player state happens on the main actor — this function
            // runs on a background executor.
            let played = await MainActor.run { self.pendingPlayed }

            // Build now_playing info for the current song
            var nowPlaying: (id: Int, startedAt: Date)?
            if let song = await MainActor.run(body: { self.currentSong }),
               let startedAt = await MainActor.run(body: { self.currentSongStartedAt }) {
                nowPlaying = (id: song.id, startedAt: startedAt)
            }

            let channelId = await MainActor.run { self.selectedChannel?.id }
            let onCellular = await MainActor.run { self.isCellular }
            let limit = ChannelCacheSettings.shared.limit(for: channelId)
            let syncBuffer = limit.requestBufferMB
            let targetDuration: Double? = limit.mode == .duration ? limit.durationSeconds : nil
            let channelName = await MainActor.run { self.channelLabel(for: channelId) }
            let syncLabel = await MainActor.run { self.syncLogReason(reason, channelId: channelId) }
            let newItems = try await api.sync(played: played, bufferCacheMB: syncBuffer, nowPlaying: nowPlaying, channelId: channelId, targetDurationSeconds: targetDuration, reason: syncLabel, silent: silent)
            await MainActor.run {
                self.loggedNotConfigured = false
                pendingPlayed.removeAll { p in played.contains { $0.id == p.id } }
                savePendingPlayed()
            }

            // Add new items to queue (skip already queued or currently playing)
            let existingIds = await MainActor.run { () -> Set<Int> in
                var ids = Set(self.queue.map(\.id))
                if let current = self.currentSong { ids.insert(current.id) }
                return ids
            }
            let toAdd = newItems.filter { !existingIds.contains($0.id) }

            if !toAdd.isEmpty {
                await MainActor.run {
                    queue.append(contentsOf: toAdd)
                    // The server has more for this channel, so it's no longer exhausted.
                    exhaustedChannelIds.remove(channelId)
                }
                // The queue just grew — possibly with songs that were already cached
                // from a previous session, not just freshly downloaded ones — so the
                // displayed stats need to reflect that regardless of what (if anything)
                // gets downloaded below.
                recalculateCacheStats()
            }

            // Download songs and artwork in background. Guarded by a per-channel lock so this
            // loop can't race another concurrent one (e.g. background prefill or Fill All
            // Caches) past the configured cache limit. Results are collected and logged as
            // one summary line rather than one line per song.
            let acquiredLock = await MainActor.run { self.beginDownloading(channelId: channelId) }
            if acquiredLock { heldLock = .some(channelId) }
            if !acquiredLock {
                AppLogger.shared.log(.playbackError, "\(channelName): already downloading elsewhere, skipped this round", details: "trigger: \(reason)")
            } else {
                // Snapshot once (cheap, in-memory) rather than re-deriving from @Published
                // state on every limit check — the disk stat checks below run off the main
                // actor so a large cache never blocks the UI.
                let channelItems = await MainActor.run { self.itemsForChannel(channelId) }

                if onCellular {
                    // On cellular: keep at most 2 low-bitrate songs cached, and never exceed
                    // this channel's configured cache limit even on Wi-Fi later.
                    let allQueued = await MainActor.run { self.queue }
                    let cachedCount = allQueued.filter { CacheManager.shared.isCached($0) }.count
                    let reachedLimit = Self.hasReachedLimit(limit, items: channelItems)
                    if cachedCount < 2 && !reachedLimit {
                        if let next = allQueued.first(where: { !CacheManager.shared.isCached($0) }) {
                            do {
                                _ = try await api.downloadSong(playlistItemId: next.id, fileExtension: next.fileExtension, lowBitrate: true, silent: true)
                                AppLogger.shared.log(.downloadSuccess, "Cached \"\(next.title)\" for \(channelName) (smaller file for cellular)", details: "trigger: \(reason)")
                                await logCacheState()
                            } catch {
                                if !isCancellation(error) {
                                    AppLogger.shared.log(.downloadFailure, "Couldn't cache \"\(next.title)\" — \(error.localizedDescription)", details: "channel: \(channelName); trigger: \(reason)")
                                }
                            }
                            if let albumId = next.albumId {
                                _ = await prefetchArtwork(albumId: albumId, api: api)
                            }
                            await MainActor.run { self.startPlaybackIfIdle() }
                        }
                    }
                } else {
                    let queued = await MainActor.run { self.queue }
                    let newIds = Set(newItems.map { $0.id })
                    let allItems = newItems + queued.filter { !newIds.contains($0.id) }
                    var downloaded = 0
                    var failed = 0
                    var consecutiveFailures = 0
                    var artworkOffline = false
                    var lastFailureReason: String?
                    for item in allItems {
                        if Task.isCancelled { break }
                        if !CacheManager.shared.isCached(item) {
                            if Self.hasReachedLimit(limit, items: channelItems) { break }
                            do {
                                _ = try await api.downloadSong(playlistItemId: item.id, fileExtension: item.fileExtension, silent: true)
                                downloaded += 1
                                consecutiveFailures = 0
                            } catch {
                                // A cancelled download just means a newer sync superseded this
                                // one — normal, expected, not worth logging as a failure.
                                if isCancellation(error) { break }
                                failed += 1
                                consecutiveFailures += 1
                                lastFailureReason = error.localizedDescription
                                if consecutiveFailures >= Self.maxConsecutiveDownloadFailures {
                                    lastFailureReason = "\(error.localizedDescription) (gave up on this pass after \(consecutiveFailures) failures in a row)"
                                    break
                                }
                            }
                            await MainActor.run { self.startPlaybackIfIdle() }
                        }
                        if let albumId = item.albumId, !artworkOffline {
                            artworkOffline = await prefetchArtwork(albumId: albumId, api: api) == .networkError
                        }
                    }
                    if !toAdd.isEmpty || downloaded > 0 || failed > 0 {
                        var parts: [String] = []
                        if !toAdd.isEmpty { parts.append("\(toAdd.count) new song\(toAdd.count == 1 ? "" : "s") found") }
                        if downloaded > 0 { parts.append("\(downloaded) cached") }
                        if failed > 0 { parts.append("\(failed) failed") }
                        var details = "trigger: \(reason)"
                        if failed > 0 { details += "; last error: \(lastFailureReason ?? "?")" }
                        AppLogger.shared.log(failed > 0 && downloaded == 0 ? .downloadFailure : .downloadSuccess, "\(channelName): \(parts.joined(separator: ", "))", details: details)
                    }
                    if downloaded > 0 {
                        await logCacheState()
                    }
                    await MainActor.run { self.cacheUpdateTick += 1 }
                    // Recompute even when nothing new downloaded this round: the queue
                    // may have just been populated (toAdd) with songs that were already
                    // cached from a previous session, and the stats need to reflect
                    // that too, not just fresh downloads.
                    recalculateCacheStats()
                }
                await MainActor.run { self.endDownloading(channelId: channelId) }
                heldLock = nil
            }

            await MainActor.run { self.startPlaybackIfIdle() }

            // Fire-and-forget: prefill every other channel's queue and pre-warm their
            // players. Single-flight — a second one started while the first is still
            // walking the channel list would just double every request.
            let shouldStartBackground = await MainActor.run { () -> Bool in
                if self.isBackgroundSyncing { return false }
                self.isBackgroundSyncing = true
                return true
            }
            if shouldStartBackground {
                Task {
                    await syncBackgroundChannels()
                    await MainActor.run { self.isBackgroundSyncing = false }
                }
            }

            return .success
        } catch {
            // The request-level failure (URL, status, error text) is already logged by
            // APIService unless silent — logging it again here just doubled every
            // entry. A cancelled sync just means a newer one superseded it — normal,
            // expected behavior (see triggerSync). Either way: cached music keeps
            // playing; performSyncWithRetry handles trying again.
            if let locked = heldLock {
                await MainActor.run { self.endDownloading(channelId: locked) }
            }
            return .failure
        }
    }

    /// If nothing is playing and something playable is on disk, start it. Safe to call
    /// from anywhere on the main actor; a no-op while a song is loaded (playing or paused).
    private func startPlaybackIfIdle() {
        guard currentSong == nil else { return }
        if playNext() {
            idleFallbackTask?.cancel()
            idleFallbackTask = nil
        } else if idleFallbackTask == nil {
            // Still nothing for this channel. Other channels may have music by now —
            // give this one a grace period (immediately if offline), then move.
            scheduleIdleFallback(immediately: !wasNetworkConnected)
        }
    }

    /// Outcome of one channel's background prefill pass, for folding into a single
    /// combined summary line rather than logging each channel separately.
    private struct PrefillResult {
        let channelName: String
        let newItemsSynced: Int
        let downloaded: Int
        let failed: Int
        let note: String?
    }

    /// Logs every channel's prefill outcome as one combined, plain-English line (skips
    /// entirely if nothing happened anywhere) instead of one line per channel.
    private func logPrefillSummary(_ prefix: String, _ results: [PrefillResult]) {
        let active = results.filter { $0.newItemsSynced > 0 || $0.downloaded > 0 || $0.failed > 0 || $0.note != nil }
        guard !active.isEmpty else { return }
        let parts = active.map { r -> String in
            var pieces: [String] = []
            if r.newItemsSynced > 0 { pieces.append("\(r.newItemsSynced) found") }
            if r.downloaded > 0 { pieces.append("\(r.downloaded) downloaded") }
            if r.failed > 0 { pieces.append("\(r.failed) failed") }
            if let note = r.note { pieces.append(note) }
            return pieces.isEmpty ? r.channelName : "\(r.channelName) (\(pieces.joined(separator: ", ")))"
        }
        let anyFailure = active.contains { $0.failed > 0 || ($0.note?.hasPrefix("sync failed") ?? false) }
        AppLogger.shared.log(anyFailure ? .downloadFailure : .downloadSuccess, "\(prefix): \(parts.joined(separator: ", "))")
    }

    private func syncBackgroundChannels() async {
        let (api, configured) = await MainActor.run { (self.apiService, self.apiService?.isConfigured ?? false) }
        guard let api, configured else { return }
        let activeId = await MainActor.run { selectedChannel?.id }
        var channelIds: [Int?] = [nil]
        channelIds += await MainActor.run { availableChannels.map { Optional($0.id) } }
        var results: [PrefillResult] = []
        var consecutiveSyncFailures = 0
        for channelId in channelIds where channelId != activeId {
            let result = await prefillBackgroundQueue(channelId: channelId, api: api, reason: "background prefill")
            results.append(result)
            // If the server can't be reached, every remaining channel would fail the
            // same way — stop after a few rather than time out once per channel.
            if result.note?.hasPrefix("sync failed") == true {
                consecutiveSyncFailures += 1
                if consecutiveSyncFailures >= Self.maxConsecutiveDownloadFailures { break }
            } else {
                consecutiveSyncFailures = 0
            }
        }
        logPrefillSummary("Preloaded songs", results)
    }

    private func prefillBackgroundQueue(channelId: Int?, api: APIService, ignoreCellular: Bool = false, reason: String) async -> PrefillResult {
        let name = await MainActor.run { self.channelLabel(for: channelId) }
        let existing = await MainActor.run { backgroundQueues[channelId] ?? [] }
        let existingIds = Set(existing.map { $0.id })

        let onCellular = await MainActor.run { isCellular }
        let limit = ChannelCacheSettings.shared.limit(for: channelId)
        let effectiveBuffer = limit.requestBufferMB
        let targetDuration: Double? = limit.mode == .duration ? limit.durationSeconds : nil
        let syncLabel = await MainActor.run { self.syncLogReason(reason, channelId: channelId) }

        let newItems: [SongItem]
        do {
            newItems = try await api.sync(
                played: [],
                bufferCacheMB: effectiveBuffer,
                nowPlaying: nil,
                channelId: channelId,
                targetDurationSeconds: targetDuration,
                reason: syncLabel,
                silent: true
            )
        } catch {
            return PrefillResult(channelName: name, newItemsSynced: 0, downloaded: 0, failed: 0, note: "sync failed: \(error.localizedDescription)")
        }

        let toAdd = newItems.filter { !existingIds.contains($0.id) }
        if !toAdd.isEmpty {
            await MainActor.run {
                var q = self.backgroundQueues[channelId] ?? []
                q.append(contentsOf: toAdd)
                self.backgroundQueues[channelId] = q
                self.exhaustedChannelIds.remove(channelId)
            }
            // The queue just grew — possibly with songs already cached from a previous
            // session, not just freshly downloaded ones — so stats need to reflect that
            // regardless of what (if anything) gets downloaded below.
            recalculateCacheStats()
        }

        // Download on WiFi at full quality; skip on cellular unless explicitly requested
        guard !onCellular || ignoreCellular else {
            return PrefillResult(channelName: name, newItemsSynced: toAdd.count, downloaded: 0, failed: 0, note: nil)
        }

        // Guarded by the same per-channel lock as performSync's download loop, so a
        // background prefill can't race the active-channel sync (or another prefill)
        // past this channel's configured cache limit.
        let acquiredLock = await MainActor.run { self.beginDownloading(channelId: channelId) }
        guard acquiredLock else {
            return PrefillResult(channelName: name, newItemsSynced: toAdd.count, downloaded: 0, failed: 0, note: "already downloading elsewhere")
        }

        var downloaded = 0
        var failed = 0
        var consecutiveFailures = 0
        var artworkOffline = false
        var lastFailureReason: String?
        // Snapshot once — the limit check below runs off the main actor so a large
        // cache never blocks the UI, and doesn't need to re-derive this every iteration.
        let channelItems = await MainActor.run { self.itemsForChannel(channelId) }
        let all = await MainActor.run { backgroundQueues[channelId] ?? [] }
        for item in all {
            if Task.isCancelled { break }
            if !CacheManager.shared.isCached(item) {
                if Self.hasReachedLimit(limit, items: channelItems) { break }
                do {
                    _ = try await api.downloadSong(playlistItemId: item.id, fileExtension: item.fileExtension, silent: true)
                    downloaded += 1
                    consecutiveFailures = 0
                } catch {
                    // A cancelled download just means a newer sync superseded this one —
                    // normal, expected, not worth reporting as a failure.
                    if isCancellation(error) { break }
                    failed += 1
                    consecutiveFailures += 1
                    lastFailureReason = error.localizedDescription
                    if consecutiveFailures >= Self.maxConsecutiveDownloadFailures { break }
                }
            }
            if let albumId = item.albumId, !artworkOffline {
                artworkOffline = await prefetchArtwork(albumId: albumId, api: api) == .networkError
            }
        }
        if downloaded > 0 {
            await logCacheState()
        }
        await MainActor.run {
            self.cacheUpdateTick += 1
            self.endDownloading(channelId: channelId)
            // A channel with music on disk again is a valid fallback target.
            if downloaded > 0 { self.exhaustedChannelIds.remove(channelId) }
            // If the active channel went quiet waiting for downloads, this channel
            // may now be a better place to be than silence.
            self.startPlaybackIfIdle()
        }
        if downloaded > 0 { recalculateCacheStats() }

        // Pre-warm a silent AVPlayer for the first cached song so channel switching is instant
        let firstCached = await MainActor.run {
            (backgroundQueues[channelId] ?? []).first { CacheManager.shared.isCached($0) }
        }
        if let song = firstCached, let ext = CacheManager.shared.cachedExtension(for: song) {
            await MainActor.run { self.prewarmIfNeeded(channelId: channelId, song: song, ext: ext) }
        }

        let note = failed > 0 ? "last error: \(lastFailureReason ?? "?")" : nil
        return PrefillResult(channelName: name, newItemsSynced: toAdd.count, downloaded: downloaded, failed: failed, note: note)
    }

    func fillAllCaches() {
        guard !isFillingCache else { return }
        isFillingCache = true
        Task {
            // Read APIService state on main thread to avoid data races with @Published properties
            let (api, configured) = await MainActor.run {
                (self.apiService, self.apiService?.isConfigured ?? false)
            }
            guard let api, configured else {
                AppLogger.shared.log(.playbackError, "Can't fill caches — server isn't set up (add it in Settings)")
                await MainActor.run { self.isFillingCache = false }
                return
            }

            // Fetch channels fresh so we fill every channel even on first run
            if let fetched = try? await api.fetchChannels() {
                await MainActor.run { self.applyFetchedChannels(fetched) }
            }

            let channels = await MainActor.run { self.availableChannels }
            var channelIds: [Int?] = [nil]
            channelIds += channels.map { Optional($0.id) }
            var results: [PrefillResult] = []
            for channelId in channelIds {
                results.append(await prefillBackgroundQueue(channelId: channelId, api: api, ignoreCellular: true, reason: "fill all caches"))
            }
            logPrefillSummary("Filled caches", results)
            await MainActor.run {
                self.isFillingCache = false
                self.cacheUpdateTick += 1
            }
        }
    }

    func refreshCacheStats(reason: String) {
        // triggerSync flushes pendingPlayed first, then syncs background channels
        // and increments cacheUpdateTick so the Settings cache display updates.
        triggerSync(reason: reason)
    }

    /// Wipes every downloaded file and all in-memory state that points at them. Views
    /// must call this rather than CacheManager.clearCache() directly, otherwise the
    /// pre-warmed players keep handles to files that no longer exist.
    func clearAllCaches() {
        CacheManager.shared.clearCache()
        prewarmedChannels.removeAll()
        artworkMemoryCache.removeAllObjects()
        artworkFailed.removeAll()
        playbackFailureCounts.removeAll()
        cacheUpdateTick += 1
        recalculateCacheStats()
        AppLogger.shared.log(.cacheState, "Cache cleared — nothing will play until songs download again")
    }

    /// Creates a silent, buffered AVPlayer for a background channel so that switching to it is near-instant.
    private func prewarmIfNeeded(channelId: Int?, song: SongItem, ext: String) {
        // Don't replace an existing pre-warmed player
        guard prewarmedChannels[channelId] == nil else { return }
        // Bound how many decoders/buffers sit idle in memory — an app killed for
        // memory in the background is just as silent as one that crashed.
        guard prewarmedChannels.count < Self.maxPrewarmedChannels else { return }
        let url = CacheManager.shared.fileURL(for: song.id, ext: ext)
        let item = AVPlayerItem(url: url)
        item.preferredForwardBufferDuration = 5
        let p = AVPlayer(playerItem: item)
        p.volume = 0  // Silent until activated on channel switch
        prewarmedChannels[channelId] = PrewarmedChannel(player: p, item: item, song: song)
    }

    /// Plays the first song in the queue whose audio is actually on disk, leaving any
    /// not-yet-downloaded songs ahead of it in place for later. Returns false — and
    /// leaves nothing loaded — if the queue holds nothing playable right now.
    ///
    /// This used to take the queue strictly in order and stop dead ("waiting to
    /// download…") on the first uncached song, even with a dozen downloaded songs
    /// sitting right behind it — which, offline, meant permanent silence.
    @discardableResult
    func playNext() -> Bool {
        // Bounded: playSong() puts a song whose file vanished back at the front of the
        // queue, so without a cap a pathological disk race could spin here.
        var attempts = 0
        while attempts < max(queue.count, 1),
              let index = queue.firstIndex(where: { CacheManager.shared.isCached($0) }) {
            attempts += 1
            let song = queue.remove(at: index)
            playSong(song)
            if currentSong != nil { return true }
        }
        currentSong = nil
        isPlaying = false
        updateNowPlaying()
        return false
    }

    func playSong(_ song: SongItem) {
        player?.pause()
        removeObservers()

        currentSong = song
        hasSyncedCurrentSong = false
        hasCheckedCurrentSongForCorruption = false
        currentSongStartedAt = Date()
        // Reset so the progress bar doesn't briefly show the previous song's
        // duration/position until the new player's first periodic tick arrives.
        currentTime = 0
        duration = 0

        // Check for both original and low-bitrate cached versions
        guard let ext = CacheManager.shared.cachedExtension(for: song) else {
            // Not cached yet — put back at front of queue; download loop will play it
            queue.insert(song, at: 0)
            currentSong = nil
            AppLogger.shared.log(.playbackError, "Waiting to download \"\(song.title)\" before it can play")
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
        idleFallbackTask?.cancel()
        idleFallbackTask = nil
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

                if !self.hasCheckedCurrentSongForCorruption {
                    self.hasCheckedCurrentSongForCorruption = true
                    self.checkForIncompleteDownload(actualDuration: dur, song: song)
                }

                // Trigger sync at 50%
                if !self.hasSyncedCurrentSong && time.seconds >= dur / 2 {
                    self.hasSyncedCurrentSong = true
                    if let current = self.currentSong {
                        let played = PlayedSong(song: current, playedAt: Date(), skipped: false)
                        self.pendingPlayed.append(played)
                        self.savePendingPlayed()
                    }
                    self.triggerSync(reason: "50% playback mark")
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

        // A file that starts fine but can't be decoded to the end (truncated write,
        // corrupt frames) never fires DidPlayToEndTime — without this the app would
        // just sit silent on it forever.
        failedObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemFailedToPlayToEndTime,
            object: playerItem,
            queue: .main
        ) { [weak self, weak playerItem] notification in
            guard let self, let playerItem, self.player?.currentItem === playerItem else { return }
            let err = notification.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
            self.handlePlaybackFailure(song: song, details: err?.localizedDescription ?? "failed to play to end")
        }

        statusObserver = playerItem.observe(\.status, options: [.new]) { [weak self, weak playerItem] item, _ in
            guard item.status == .failed else { return }
            let desc = item.error?.localizedDescription ?? "unknown error"
            // KVO can deliver off the main thread; all player state lives on main.
            DispatchQueue.main.async {
                guard let self, let playerItem, self.player?.currentItem === playerItem else { return }
                self.handlePlaybackFailure(song: song, details: desc)
            }
        }
    }

    /// Recovers from an AVPlayerItem that can't play. First failure: rebuild the player
    /// once (items can fail after a long background stint for reasons that have nothing
    /// to do with the file). Second failure: the file is bad — delete it so the next
    /// sync fetches a fresh copy, and move on so the music keeps going.
    private func handlePlaybackFailure(song: SongItem, details: String) {
        guard currentSong?.id == song.id else { return }
        let attempts = (playbackFailureCounts[song.id] ?? 0) + 1
        playbackFailureCounts[song.id] = attempts
        if attempts == 1, CacheManager.shared.isCached(song) {
            AppLogger.shared.log(.playbackError, "Couldn't play \"\(song.title)\" — retrying once", details: details)
            playSong(song)
            return
        }
        playbackFailureCounts[song.id] = nil
        AppLogger.shared.log(.downloadFailure, "\"\(song.title)\" won't play — removing the bad file and moving on", details: details)
        dropCurrentSongAndAdvance(song, reason: "unplayable file")
    }

    /// Removes the current song's cached audio (without reporting it as played or
    /// skipped — the user never heard it) and advances to whatever is playable next.
    private func dropCurrentSongAndAdvance(_ song: SongItem, reason: String) {
        removeCachedFiles(for: song)
        player?.pause()
        removeObservers()
        currentSong = nil
        if !playNext() {
            handleChannelRanDry()
        }
        triggerSync(reason: reason)
        recalculateCacheStats()
    }

    private func applyReplayGain(_ song: SongItem, to avPlayer: AVPlayer) {
        if let gainDB = song.replaygainTrackGain, gainDB.isFinite {
            avPlayer.volume = Float(min(max(pow(10.0, gainDB / 20.0), 0.0), 1.0))
        } else {
            avPlayer.volume = 1.0
        }
    }

    private func songDidFinish() {
        guard let song = currentSong else { return }
        if !hasSyncedCurrentSong {
            let played = PlayedSong(song: song, playedAt: Date(), skipped: false)
            pendingPlayed.append(played)
            savePendingPlayed()
        }
        AppLogger.shared.log(.trackPlayed, "Played: \"\(song.title)\" by \(song.artist)")
        removeCachedFiles(for: song)
        playbackFailureCounts[song.id] = nil
        triggerSync(reason: "song finished")
        if !playNext() {
            handleChannelRanDry()
        }
        recalculateCacheStats()
    }

    /// The active channel has nothing playable right now (queue empty, or everything
    /// left in it is still waiting to download). Music must not stop while another
    /// channel has downloaded songs, so: if nothing more is coming for this channel or
    /// there's no connection, move immediately; otherwise give the download loop a
    /// short grace period first so a slow download doesn't bounce the user around.
    private func handleChannelRanDry() {
        let channelId = selectedChannel?.id
        if queue.isEmpty {
            exhaustedChannelIds.insert(channelId)
        }
        currentSong = nil
        isPlaying = false
        updateNowPlaying()
        scheduleIdleFallback(immediately: queue.isEmpty || !wasNetworkConnected)
    }

    private func scheduleIdleFallback(immediately: Bool) {
        idleFallbackTask?.cancel()
        idleFallbackTask = Task { @MainActor [weak self] in
            if !immediately {
                try? await Task.sleep(nanoseconds: Self.idleFallbackGraceSeconds * 1_000_000_000)
                guard !Task.isCancelled else { return }
            }
            guard let self else { return }
            self.idleFallbackTask = nil
            guard self.currentSong == nil else { return }
            self.switchToAnyChannelWithMusic()
        }
    }

    /// Moves playback to the first non-exhausted channel that has downloaded songs.
    private func switchToAnyChannelWithMusic() {
        let current = selectedChannel?.id
        if let next = channelWithPlayableSongs(excluding: [current]) {
            AppLogger.shared.log(.trackPlayed, "\(channelLabel(for: current)) has nothing downloaded to play — switching to \(channelLabel(for: next?.id))")
            selectChannel(next, userInitiated: false)
        } else {
            AppLogger.shared.log(.playbackError, "Nothing downloaded on any channel — playback resumes as soon as a song downloads")
        }
    }

    /// Whether any song known for this channel has audio on disk.
    private func hasPlayableSongs(channelId: Int?) -> Bool {
        itemsForChannel(channelId).contains { CacheManager.shared.isCached($0) }
    }

    /// First channel (All Music first, then the server's order) that isn't exhausted,
    /// isn't excluded, and has at least one downloaded song. `.some(nil)` is All Music.
    private func channelWithPlayableSongs(excluding: Set<Int?>) -> Channel?? {
        let candidates: [Channel?] = [nil] + availableChannels
        for candidate in candidates {
            let id = candidate?.id
            if excluding.contains(id) || exhaustedChannelIds.contains(id) { continue }
            if hasPlayableSongs(channelId: id) { return .some(candidate) }
        }
        return nil
    }

    private func removeCachedFiles(for song: SongItem) {
        CacheManager.shared.removeFile(for: song.id, ext: song.fileExtension)
        if song.fileExtension != "mp3" {
            CacheManager.shared.removeFile(for: song.id, ext: "mp3")
        }
    }

    /// Detects a cached file that decoded to a noticeably shorter duration than the
    /// server's metadata says it should be — the signature of a truncated/corrupt
    /// download left in the cache (e.g. from an interrupted transfer, or a disk-full
    /// write). Logs it, removes the bad file so a fresh copy gets fetched next sync,
    /// and skips ahead immediately rather than let the user sit through a clipped track.
    private func checkForIncompleteDownload(actualDuration: Double, song: SongItem) {
        guard let expected = song.duration, expected.isFinite, expected > 5 else { return }
        guard actualDuration < expected * 0.9 else { return }
        AppLogger.shared.log(
            .downloadFailure,
            "\"\(song.title)\" only partly downloaded — re-downloading it",
            details: "expected \(Int(expected))s of audio, got \(Int(actualDuration))s"
        )
        // This runs inside the periodic time observer's callback; tearing the observer
        // down from inside its own callback is asking for a deadlock, so hop out first.
        DispatchQueue.main.async { [weak self] in
            guard let self, self.currentSong?.id == song.id else { return }
            self.dropCurrentSongAndAdvance(song, reason: "incomplete file")
        }
    }

    func play() {
        do {
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            AppLogger.shared.log(.playbackError, "Couldn't resume audio", details: error.localizedDescription)
        }
        if currentSong == nil {
            idleFallbackTask?.cancel()
            idleFallbackTask = nil
            if playNext() { return }
            // The user asked for music and this channel has nothing downloaded — play
            // whatever channel does, rather than sit silent waiting on the network.
            if let alt = channelWithPlayableSongs(excluding: [selectedChannel?.id]) {
                AppLogger.shared.log(.trackPlayed, "Nothing downloaded for \(channelLabel(for: selectedChannel?.id)) — playing \(channelLabel(for: alt?.id)) instead")
                selectChannel(alt, userInitiated: false)
            } else {
                AppLogger.shared.log(.playbackError, "Nothing downloaded yet on any channel — waiting for the first song to download")
                triggerSync(reason: "play tapped — nothing cached")
            }
            return
        }
        guard let player else {
            // A loaded song with no player shouldn't be possible; rebuild rather than
            // leave the play button doing nothing.
            if let song = currentSong {
                AppLogger.shared.log(.playbackError, "Restarting playback of \"\(song.title)\"")
                playSong(song)
            }
            return
        }
        // If the player item has failed (can happen after a long background session),
        // recreate the player for the current song rather than calling play() on a broken instance.
        if let item = player.currentItem, item.status == .failed {
            let desc = item.error?.localizedDescription ?? "unknown"
            AppLogger.shared.log(.playbackError, "Restarted playback of \"\(currentSong?.title ?? "current song")\" after a playback error", details: desc)
            if let song = currentSong {
                playSong(song)
            }
            return
        }
        player.play()
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
                savePendingPlayed()
            }
            AppLogger.shared.log(.trackSkipped, "Skipped: \"\(song.title)\" by \(song.artist)")
            removeCachedFiles(for: song)
            playbackFailureCounts[song.id] = nil
        }
        if !playNext() {
            handleChannelRanDry()
        }
        triggerSync(reason: "song skipped")
        recalculateCacheStats()
    }

    func seek(to fraction: Double) {
        guard duration > 0, fraction.isFinite else { return }
        let time = CMTime(seconds: min(max(fraction, 0), 1) * duration, preferredTimescale: 600)
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
            if let dur = song.duration, dur.isFinite {
                info[MPMediaItemPropertyPlaybackDuration] = dur
            }
            if let year = song.year {
                info[MPMediaItemPropertyAlbumTrackNumber] = year
            }
            info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = currentTime.isFinite ? currentTime : 0
            info[MPNowPlayingInfoPropertyPlaybackRate] = isPlaying ? 1.0 : 0.0
            if let artwork = currentArtwork {
                info[MPMediaItemPropertyArtwork] = artwork
            }
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    func refreshNowPlaying() {
        updateNowPlaying()
    }

    // MARK: - Artwork

    /// Cover art for an album from the on-disk cache (via a small memory cache), or nil
    /// if it has never been downloaded. Never touches the network.
    func artworkImage(for albumId: Int) -> UIImage? {
        let key = albumId as NSNumber
        if let image = artworkMemoryCache.object(forKey: key) { return image }
        guard let image = CacheManager.shared.cachedArtwork(for: albumId) else { return nil }
        artworkMemoryCache.setObject(image, forKey: key)
        return image
    }

    func loadArtworkForCurrentSong() {
        guard let song = currentSong, let albumId = song.albumId,
              let image = artworkImage(for: albumId) else {
            currentArtwork = nil
            currentArtworkImage = nil
            updateNowPlaying()
            return
        }
        currentArtworkImage = image
        currentArtwork = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
        updateNowPlaying()
    }

    private enum ArtworkFetchOutcome {
        case done
        case networkError
    }

    /// Downloads an album's cover to the disk cache if it isn't there already. Returns
    /// `.networkError` so callers can stop asking for artwork for the rest of a pass
    /// when the server is unreachable — artwork is cosmetic and must never hold up
    /// (or repeatedly time out inside) a song download loop.
    private func prefetchArtwork(albumId: Int, api: APIService) async -> ArtworkFetchOutcome {
        if Task.isCancelled { return .done }
        if CacheManager.shared.hasArtwork(for: albumId) { return .done }
        let knownMissing = await MainActor.run { self.artworkFailed.contains(albumId) }
        if knownMissing { return .done }

        do {
            let (data, status) = try await api.fetchCoverArt(albumId: albumId)
            if status == 404 {
                await MainActor.run { _ = self.artworkFailed.insert(albumId) }
                return .done
            }
            guard status == 200, let image = UIImage(data: data) else {
                await MainActor.run { _ = self.artworkFailed.insert(albumId) }
                return .done
            }
            CacheManager.shared.saveArtwork(image, for: albumId)
            await MainActor.run {
                if self.currentSong?.albumId == albumId { self.loadArtworkForCurrentSong() }
            }
            return .done
        } catch {
            // Network error — not marked as failed so it can retry next sync
            return isCancellation(error) ? .done : .networkError
        }
    }

    private func logCacheState() async {
        let count = CacheManager.shared.cacheFileCount()
        let sizeMB = CacheManager.shared.totalCacheSizeMB()

        let (allQueue, allBg, current) = await MainActor.run {
            (self.queue, self.backgroundQueues, self.currentSong)
        }

        var seen = Set<Int>()
        var runtimeSeconds = 0.0
        let allItems = allQueue + allBg.values.flatMap { $0 } + [current].compactMap { $0 }
        for item in allItems {
            guard seen.insert(item.id).inserted else { continue }
            if CacheManager.shared.isCached(item), let d = item.duration, d.isFinite {
                runtimeSeconds += d
            }
        }

        let total = Int(min(max(runtimeSeconds, 0), 1e9))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        let runtimeStr: String
        if hours > 0 {
            runtimeStr = "\(hours)h \(minutes)m"
        } else if minutes > 0 {
            runtimeStr = "\(minutes)m \(seconds)s"
        } else {
            runtimeStr = "\(seconds)s"
        }

        let sizeStr = sizeMB >= 1024
            ? String(format: "%.1f GB", sizeMB / 1024)
            : String(format: "%.0f MB", sizeMB)

        AppLogger.shared.log(.cacheState, "Cache now has \(count) songs (\(runtimeStr), \(sizeStr))")
    }

    /// Cache status for a single channel: how much of it is currently downloaded on-device.
    struct ChannelCacheStats: Codable {
        let name: String
        let channelId: Int?
        let sizeBytes: Int64
        let songCount: Int
        let durationSeconds: Double
    }

    /// Statistics cache: the source views read from (ChannelsView, SettingsView,
    /// NowPlayingView). Computing this involves real disk stat calls per cached file,
    /// which used to run synchronously on every SwiftUI render — that's what made the
    /// Channels page slow once channels held hundreds of cached items. Now it's only
    /// recomputed when cache state actually changes (see recalculateCacheStats callers),
    /// off the main thread, and views just read this stored array.
    ///
    /// Also persisted to disk (see loadPersistedCacheStats/persistCacheStats) so launch
    /// shows the last known numbers immediately instead of zeros while the in-memory
    /// queues are still empty and the real recompute (below) hasn't finished yet.
    @Published private(set) var cachedChannelStats: [ChannelCacheStats] = []

    private static var cachedStatsFileURL: URL {
        AppPaths.file("channel_cache_stats.json")
    }

    /// Loads the last-persisted stats synchronously so the UI has real numbers to show
    /// immediately at launch, before the in-memory queues are even populated.
    private func loadPersistedCacheStats() {
        guard let data = try? Data(contentsOf: Self.cachedStatsFileURL),
              let saved = try? JSONDecoder().decode([ChannelCacheStats].self, from: data) else { return }
        cachedChannelStats = saved
    }

    private func persistCacheStats(_ stats: [ChannelCacheStats]) {
        guard let data = try? JSONEncoder().encode(stats) else { return }
        try? data.write(to: Self.cachedStatsFileURL, options: .atomic)
    }

    /// Recomputes cachedChannelStats off the main thread, publishes the result, and
    /// persists it to disk. Call this whenever cache state actually changes — not from
    /// view bodies.
    func recalculateCacheStats() {
        Task {
            let entries: [(name: String, channelId: Int?, items: [SongItem])] = await MainActor.run {
                var result: [(String, Int?, [SongItem])] = [("All Music", nil, self.itemsForChannel(nil))]
                var seen: Set<Int?> = [nil]
                for ch in self.availableChannels {
                    result.append((ch.name, ch.id, self.itemsForChannel(ch.id)))
                    seen.insert(ch.id)
                }
                // Channels we hold songs for but that aren't in the (possibly stale or
                // never-loaded) channel list. They MUST be included: this list is what
                // gets persisted as the offline song library, and dropping them here
                // used to silently erase every non-All-Music channel's metadata on an
                // offline launch — orphaning their cached audio for good.
                for key in self.backgroundQueues.keys where !seen.contains(key) {
                    result.append((self.channelLabel(for: key), key, self.itemsForChannel(key)))
                }
                return result
            }
            // This is the one place every channel's known song metadata is already
            // gathered, so piggyback the offline-recovery persistence here rather than
            // re-deriving it elsewhere. See resumeFromDiskCacheIfNeeded().
            persistSongLibrary(entries.map { (channelId: $0.channelId, items: $0.items) })
            // The disk stat calls happen here, off the main actor.
            let computed = entries.map { Self.computeStats(name: $0.0, channelId: $0.1, items: $0.2) }
            await MainActor.run { self.cachedChannelStats = computed }
            persistCacheStats(computed)
        }
    }

    // MARK: - Offline channel persistence

    /// The channel list and the selected channel are persisted so the Channels tab,
    /// the CarPlay list, next/previous tuning and the "resume where I left off" logic
    /// all work on a launch with no connectivity. Without this, an offline launch had
    /// an empty channel list ("Loading channels…" forever) and always fell back to
    /// All Music — even when the channel the user was actually on had an hour cached.
    private static var channelsFileURL: URL {
        AppPaths.file("channels.json")
    }
    private static let selectedChannelKey = "selectedChannel"

    private func persistChannels() {
        guard let data = try? JSONEncoder().encode(availableChannels) else { return }
        try? data.write(to: Self.channelsFileURL, options: .atomic)
    }

    private func persistSelectedChannel() {
        if let channel = selectedChannel, let data = try? JSONEncoder().encode(channel) {
            UserDefaults.standard.set(data, forKey: Self.selectedChannelKey)
        } else {
            UserDefaults.standard.removeObject(forKey: Self.selectedChannelKey)
        }
    }

    private func loadPersistedChannels() {
        if let data = try? Data(contentsOf: Self.channelsFileURL),
           let saved = try? JSONDecoder().decode([Channel].self, from: data) {
            availableChannels = saved
        }
        if let data = UserDefaults.standard.data(forKey: Self.selectedChannelKey),
           let saved = try? JSONDecoder().decode(Channel.self, from: data) {
            selectedChannel = saved
        }
    }

    // MARK: - Offline song library persistence

    /// Song metadata (title/artist/duration/etc.) has no home on disk of its own — the
    /// on-disk audio cache (CacheManager) stores files keyed only by song ID. Without
    /// this, a fresh launch with zero connectivity has no way to know what those cached
    /// files even are, so nothing could ever play. See resumeFromDiskCacheIfNeeded().
    private struct PersistedChannelSongs: Codable {
        let channelId: Int?
        let items: [SongItem]
    }

    private static var songLibraryFileURL: URL {
        AppPaths.file("song_library.json")
    }

    private func persistSongLibrary(_ entries: [(channelId: Int?, items: [SongItem])]) {
        let persisted = entries.map { PersistedChannelSongs(channelId: $0.channelId, items: $0.items) }
        guard let data = try? JSONEncoder().encode(persisted) else { return }
        try? data.write(to: Self.songLibraryFileURL, options: .atomic)
    }

    /// Loads last-known song metadata into backgroundQueues, keeping only entries whose
    /// audio file is still actually present on disk — metadata alone isn't playable,
    /// and the cache may have evicted files since the metadata was last persisted.
    private func loadPersistedSongLibrary() {
        guard let data = try? Data(contentsOf: Self.songLibraryFileURL),
              let persisted = try? JSONDecoder().decode([PersistedChannelSongs].self, from: data) else { return }
        var totalKnown = 0
        var totalPlayable = 0
        for entry in persisted {
            let cachedItems = entry.items.filter { CacheManager.shared.isCached($0) }
            totalKnown += entry.items.count
            totalPlayable += cachedItems.count
            guard !cachedItems.isEmpty else { continue }
            backgroundQueues[entry.channelId] = cachedItems
        }
        // The library listed songs but not one of their audio files is on disk. Since
        // audio now lives in un-purgeable storage this should only happen after an
        // upgrade from the old build (files were in .cachesDirectory and got purged) or
        // a manual cache clear — either way, surface it instead of silently showing an
        // empty queue and "waiting to sync" forever.
        if totalKnown > 0 && totalPlayable == 0 {
            AppLogger.shared.log(.playbackError, "The system had cleared \(totalKnown) downloaded song\(totalKnown == 1 ? "" : "s") to free up space — re-downloading now; music resumes once the first one is back")
        }
    }

    /// All known items for a channel (live queue + prefilled songs if it's the active
    /// channel, otherwise its background queue). Cheap in-memory lookup — no disk I/O.
    private func itemsForChannel(_ channelId: Int?) -> [SongItem] {
        if channelId == selectedChannel?.id {
            var active = queue
            if let song = currentSong { active.insert(song, at: 0) }
            let activeIds = Set(active.map(\.id))
            let prefilled = (backgroundQueues[channelId] ?? []).filter { !activeIds.contains($0.id) }
            return active + prefilled
        } else {
            return backgroundQueues[channelId] ?? []
        }
    }

    /// Does the real disk stat work (hasCached / fileSizeBytes per item). Pure and
    /// actor-independent so it's safe to call from a background thread.
    private static func computeStats(name: String, channelId: Int?, items: [SongItem]) -> ChannelCacheStats {
        var sizeBytes: Int64 = 0
        var songCount = 0
        var durationSeconds = 0.0
        for item in items {
            guard CacheManager.shared.isCached(item) else { continue }
            sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: item.fileExtension)
            if item.fileExtension != "mp3" {
                sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: "mp3")
            }
            songCount += 1
            if let d = item.duration, d.isFinite { durationSeconds += d }
        }
        return ChannelCacheStats(name: name, channelId: channelId, sizeBytes: sizeBytes, songCount: songCount, durationSeconds: durationSeconds)
    }

    /// Whether `items` (a snapshot of one channel's known songs) already satisfy
    /// `limit`. Used inside download loops — takes a snapshot instead of re-deriving
    /// it from @Published state, and is actor-independent so the disk stat calls run
    /// off the main thread.
    private static func hasReachedLimit(_ limit: ChannelCacheLimit, items: [SongItem]) -> Bool {
        var sizeBytes: Int64 = 0
        var durationSeconds = 0.0
        for item in items {
            guard CacheManager.shared.isCached(item) else { continue }
            switch limit.mode {
            case .size:
                sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: item.fileExtension)
                if item.fileExtension != "mp3" {
                    sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: "mp3")
                }
            case .duration:
                if let d = item.duration, d.isFinite { durationSeconds += d }
            }
        }
        switch limit.mode {
        case .duration: return durationSeconds >= limit.durationSeconds
        case .size: return sizeBytes >= limit.sizeBytes
        }
    }

    /// Claims the download lock for a channel. Returns false if another loop already
    /// holds it, meaning the caller should skip downloading this round rather than race.
    private func beginDownloading(channelId: Int?) -> Bool {
        guard !downloadingChannelIds.contains(channelId) else { return false }
        downloadingChannelIds.insert(channelId)
        return true
    }

    private func endDownloading(channelId: Int?) {
        downloadingChannelIds.remove(channelId)
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
        if let obs = failedObserver {
            NotificationCenter.default.removeObserver(obs)
            failedObserver = nil
        }
        statusObserver?.invalidate()
        statusObserver = nil
    }

    // MARK: - Pending played persistence

    private static let pendingPlayedKey = "pendingPlayedSongs"

    func savePendingPlayed() {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        if let data = try? encoder.encode(pendingPlayed) {
            UserDefaults.standard.set(data, forKey: Self.pendingPlayedKey)
        }
    }

    func loadPendingPlayed() {
        guard let data = UserDefaults.standard.data(forKey: Self.pendingPlayedKey) else { return }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        if let saved = try? decoder.decode([PlayedSong].self, from: data) {
            pendingPlayed = saved
        }
    }

    deinit {
        removeObservers()
        syncRetryTask?.cancel()
        idleFallbackTask?.cancel()
        configChangeTask?.cancel()
        networkMonitor.cancel()
    }
}
