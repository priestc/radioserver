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

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var statusObserver: NSKeyValueObservation?
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
    private var hasCheckedCurrentSongForCorruption = false
    private var currentSongStartedAt: Date?
    private var syncRetryTask: Task<Void, Never>?
    private var syncBackoffSeconds: Double = 2

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
        // Show the last known numbers immediately (in-memory queues are still empty
        // right now, so a real recompute at this point would show all zeros) — then
        // kick off a real recompute in the background to correct/refresh them.
        loadPersistedCacheStats()
        recalculateCacheStats()
        AppLogger.shared.log(.startup, "App started")
        // CRITICAL, NEVER REMOVE: this app must be able to play music with zero
        // network connectivity, using only what's already downloaded to disk. See the
        // "offline playback is non-negotiable" note above performSync() for the full
        // rule. This call is what makes that possible at launch — it must run
        // synchronously here, before any network call is attempted, not after one
        // fails or times out.
        resumeFromDiskCacheIfNeeded()
    }

    /// Reconstructs the playback queue for the currently selected channel from
    /// whatever song metadata + cached audio survived from the previous session, and
    /// starts playback immediately if anything is playable — all before a single
    /// network request has been made. Without this, a fully offline launch has no way
    /// to know what's already sitting in the on-disk cache (the cache stores raw audio
    /// files keyed only by song ID; it has no title/artist/etc. metadata of its own),
    /// so the queue would stay empty forever and music would never play, no matter how
    /// much is cached. See CLAUDE.md: "the app must always play music, even with no
    /// internet connection, as long as at least one song exists in the cache."
    private func resumeFromDiskCacheIfNeeded() {
        loadPersistedSongLibrary()
        queue = backgroundQueues[selectedChannel?.id] ?? []
        guard currentSong == nil, !queue.isEmpty else { return }
        AppLogger.shared.log(.trackPlayed, "Resuming playback from cache while syncing with server")
        playNext()
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
                    AppLogger.shared.log(.playbackError, "Lost internet connection — downloads paused until it's back")
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
        // Perform an initial sync to populate the queue
        Task { await performSync(reason: "app launch") }
    }

    /// Set when a channel fetch fails, so views can show it — nil while loading or once
    /// a fetch has succeeded.
    @Published var channelsError: String?

    /// The single owner of "/channels/" fetches. Views (e.g. ChannelsView) should call
    /// this rather than hitting APIService directly — otherwise the app-launch fetch
    /// here and a view's own fetch can fire at the same moment, doubling the request.
    func fetchChannels() {
        guard let api = apiService else { return }
        Task {
            do {
                let channels = try await api.fetchChannels()
                await MainActor.run {
                    self.availableChannels = channels
                    self.channelsError = nil
                }
                recalculateCacheStats()
                // Do NOT call syncBackgroundChannels() here — background syncs
                // must only run after an active sync has sent pendingPlayed,
                // otherwise the server returns already-played songs for background channels.
            } catch {
                await MainActor.run { self.channelsError = error.localizedDescription }
            }
        }
    }

    func selectNextChannel() {
        let available: [Channel?] = ([nil] + availableChannels).filter { !exhaustedChannelIds.contains($0?.id) }
        guard !available.isEmpty else { return }
        let currentIndex = available.firstIndex(where: { $0 == selectedChannel }) ?? -1
        let nextIndex = (currentIndex + 1) % available.count
        selectChannel(available[nextIndex])
    }

    func selectPreviousChannel() {
        let available: [Channel?] = ([nil] + availableChannels).filter { !exhaustedChannelIds.contains($0?.id) }
        guard !available.isEmpty else { return }
        let currentIndex = available.firstIndex(where: { $0 == selectedChannel }) ?? available.count
        let prevIndex = (currentIndex - 1 + available.count) % available.count
        selectChannel(available[prevIndex])
    }

    func selectChannel(_ channel: Channel?) {
        guard !exhaustedChannelIds.contains(channel?.id) else { return }
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
        hasCheckedCurrentSongForCorruption = false
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

    func triggerSync(reason: String) {
        syncRetryTask?.cancel()
        syncBackoffSeconds = 2
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

    private func performSyncWithRetry(reason: String) async {
        while !Task.isCancelled {
            let success = await performSync(reason: reason)
            if success { return }

            // Backoff and retry
            let delay = syncBackoffSeconds
            syncBackoffSeconds = min(syncBackoffSeconds * 2, 60)
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
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
    private func performSync(reason: String) async -> Bool {
        guard let api = apiService, api.isConfigured else {
            AppLogger.shared.log(.playbackError, "Can't sync yet — server isn't set up")
            return false
        }

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
            let limit = ChannelCacheSettings.shared.limit(for: channelId)
            let syncBuffer = limit.requestBufferMB
            let targetDuration: Double? = limit.mode == .duration ? limit.durationSeconds : nil
            let channelName = await MainActor.run { self.channelLabel(for: channelId) }
            let syncLabel = await MainActor.run { self.syncLogReason(reason, channelId: channelId) }
            let newItems = try await api.sync(played: played, bufferCacheMB: syncBuffer, nowPlaying: nowPlaying, channelId: channelId, targetDurationSeconds: targetDuration, reason: syncLabel)
            await MainActor.run {
                pendingPlayed.removeAll { p in played.contains { $0.id == p.id } }
                savePendingPlayed()
            }

            // Add new items to queue (skip already queued)
            let existingIds = Set(await MainActor.run { self.queue.map(\.id) })
            let toAdd = newItems.filter { !existingIds.contains($0.id) }

            if !toAdd.isEmpty {
                await MainActor.run { queue.append(contentsOf: toAdd) }
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
                    let cachedCount = allQueued.filter {
                        CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3") ||
                        CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension)
                    }.count
                    let reachedLimit = Self.hasReachedLimit(limit, items: channelItems)
                    if cachedCount < 2 && !reachedLimit {
                        if let next = allQueued.first(where: {
                            !CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3") &&
                            !CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension)
                        }) {
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
                    var downloaded = 0
                    var failed = 0
                    var lastFailureReason: String?
                    for item in allItems {
                        if !CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) {
                            if Self.hasReachedLimit(limit, items: channelItems) { break }
                            do {
                                _ = try await api.downloadSong(playlistItemId: item.id, fileExtension: item.fileExtension, silent: true)
                                downloaded += 1
                            } catch {
                                // A cancelled download just means a newer sync superseded this
                                // one — normal, expected, not worth logging as a failure.
                                if isCancellation(error) { break }
                                failed += 1
                                lastFailureReason = error.localizedDescription
                            }
                            let idle = await MainActor.run { self.currentSong == nil && !self.queue.isEmpty }
                            if idle { await MainActor.run { self.playNext() } }
                        }
                        if let albumId = item.albumId {
                            await prefetchArtwork(albumId: albumId, api: api)
                        }
                    }
                    if !toAdd.isEmpty || downloaded > 0 || failed > 0 {
                        var parts: [String] = []
                        if !toAdd.isEmpty { parts.append("\(toAdd.count) new song\(toAdd.count == 1 ? "" : "s") found") }
                        if downloaded > 0 { parts.append("\(downloaded) cached") }
                        if failed > 0 { parts.append("\(failed) failed") }
                        var details = "trigger: \(reason)"
                        if failed > 0 { details += "; last error: \(lastFailureReason ?? "?")" }
                        AppLogger.shared.log(.downloadSuccess, "\(channelName): \(parts.joined(separator: ", "))", details: details)
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
            // A cancelled sync just means a newer one superseded it — that's normal,
            // expected behavior (see triggerSync), not an error worth logging.
            if !isCancellation(error) {
                AppLogger.shared.log(.apiFailure, "Sync failed — \(error.localizedDescription)", details: "trigger: \(reason)")
            }
            return false
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
        AppLogger.shared.log(.downloadSuccess, "\(prefix): \(parts.joined(separator: ", "))")
    }

    private func syncBackgroundChannels() async {
        guard let api = apiService, api.isConfigured else { return }
        let activeId = await MainActor.run { selectedChannel?.id }
        var channelIds: [Int?] = [nil]
        channelIds += await MainActor.run { availableChannels.map { Optional($0.id) } }
        var results: [PrefillResult] = []
        for channelId in channelIds where channelId != activeId {
            results.append(await prefillBackgroundQueue(channelId: channelId, api: api, reason: "background prefill"))
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
        var lastFailureReason: String?
        // Snapshot once — the limit check below runs off the main actor so a large
        // cache never blocks the UI, and doesn't need to re-derive this every iteration.
        let channelItems = await MainActor.run { self.itemsForChannel(channelId) }
        let all = await MainActor.run { backgroundQueues[channelId] ?? [] }
        for item in all {
            if !CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) {
                if Self.hasReachedLimit(limit, items: channelItems) { break }
                do {
                    _ = try await api.downloadSong(playlistItemId: item.id, fileExtension: item.fileExtension, silent: true)
                    downloaded += 1
                } catch {
                    // A cancelled download just means a newer sync superseded this one —
                    // normal, expected, not worth reporting as a failure.
                    if isCancellation(error) { break }
                    failed += 1
                    lastFailureReason = error.localizedDescription
                }
            }
            if let albumId = item.albumId {
                await prefetchArtwork(albumId: albumId, api: api)
            }
        }
        if downloaded > 0 {
            await logCacheState()
        }
        await MainActor.run {
            self.cacheUpdateTick += 1
            self.endDownloading(channelId: channelId)
        }
        if downloaded > 0 { recalculateCacheStats() }

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
                await MainActor.run { self.isFillingCache = false }
                return
            }

            // Fetch channels fresh so we fill every channel even on first run
            if let fetched = try? await api.fetchChannels() {
                await MainActor.run { self.availableChannels = fetched }
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
        hasCheckedCurrentSongForCorruption = false
        currentSongStartedAt = Date()
        // Reset so the progress bar doesn't briefly show the previous song's
        // duration/position until the new player's first periodic tick arrives.
        currentTime = 0
        duration = 0

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

        statusObserver = playerItem.observe(\.status, options: [.new]) { [weak self] item, _ in
            if item.status == .failed {
                let desc = item.error?.localizedDescription ?? "unknown error"
                AppLogger.shared.log(.playbackError, "Couldn't play \"\(song.title)\" — stuck until you reopen the app or skip", details: desc)
            }
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
            savePendingPlayed()
        }
        AppLogger.shared.log(.trackPlayed, "Played: \"\(song.title)\" by \(song.artist)")
        removeCachedFiles(for: song)
        triggerSync(reason: "song finished")
        if queue.isEmpty {
            exhaustedChannelIds.insert(selectedChannel?.id)
            autoSwitchToAvailableChannel()
        } else {
            playNext()
        }
    }

    private func autoSwitchToAvailableChannel() {
        let allChannels: [Channel?] = [nil] + availableChannels
        let available = allChannels.filter { !exhaustedChannelIds.contains($0?.id) }
        if let next = available.first {
            selectChannel(next)
        } else {
            AppLogger.shared.log(.playbackError, "Nothing left to play on any channel — playback stopped")
            currentSong = nil
            isPlaying = false
            updateNowPlaying()
        }
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
        guard let expected = song.duration, expected > 5 else { return }
        guard actualDuration < expected * 0.9 else { return }
        AppLogger.shared.log(
            .downloadFailure,
            "\"\(song.title)\" only partly downloaded — re-downloading it",
            details: "expected \(Int(expected))s of audio, got \(Int(actualDuration))s"
        )
        removeCachedFiles(for: song)
        skipToNext()
    }

    func play() {
        do {
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            AppLogger.shared.log(.playbackError, "Couldn't resume audio", details: error.localizedDescription)
        }
        if currentSong == nil {
            if queue.isEmpty {
                AppLogger.shared.log(.playbackError, "Nothing ready to play yet — waiting for songs to sync")
                triggerSync(reason: "play tapped — queue empty")
            } else {
                playNext()
            }
            return
        }
        guard let player else {
            AppLogger.shared.log(.playbackError, "Restarting playback of \"\(currentSong?.title ?? "current song")\"")
            currentSong = nil
            triggerSync(reason: "play tapped — player was nil")
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
        }
        playNext()
        triggerSync(reason: "song skipped")
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

    func refreshNowPlaying() {
        updateNowPlaying()
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
            await MainActor.run {
                self.artworkCache[albumId] = diskImage
                if self.currentSong?.albumId == albumId { self.loadArtworkForCurrentSong() }
            }
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
                await MainActor.run {
                    self.artworkCache[albumId] = image
                    if self.currentSong?.albumId == albumId { self.loadArtworkForCurrentSong() }
                }
            } else {
                await MainActor.run { self.artworkFailed.insert(albumId) }
            }
        } catch {
            // Network error — don't mark as failed so it can retry next sync
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
            let cached = CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) ||
                         CacheManager.shared.hasCached(playlistItemId: item.id, ext: "mp3")
            if cached, let d = item.duration {
                runtimeSeconds += d
            }
        }

        let hours = Int(runtimeSeconds) / 3600
        let minutes = (Int(runtimeSeconds) % 3600) / 60
        let seconds = Int(runtimeSeconds) % 60
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
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return dir.appendingPathComponent("channel_cache_stats.json")
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
                for ch in self.availableChannels {
                    result.append((ch.name, ch.id, self.itemsForChannel(ch.id)))
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
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return dir.appendingPathComponent("song_library.json")
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
        for entry in persisted {
            let cachedItems = entry.items.filter {
                CacheManager.shared.hasCached(playlistItemId: $0.id, ext: $0.fileExtension) ||
                CacheManager.shared.hasCached(playlistItemId: $0.id, ext: "mp3")
            }
            guard !cachedItems.isEmpty else { continue }
            backgroundQueues[entry.channelId] = cachedItems
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
            let isCached = CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) ||
                           CacheManager.shared.hasCached(playlistItemId: item.id, ext: "mp3")
            guard isCached else { continue }
            sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: item.fileExtension)
            if item.fileExtension != "mp3" {
                sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: "mp3")
            }
            songCount += 1
            durationSeconds += item.duration ?? 0
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
            let isCached = CacheManager.shared.hasCached(playlistItemId: item.id, ext: item.fileExtension) ||
                           CacheManager.shared.hasCached(playlistItemId: item.id, ext: "mp3")
            guard isCached else { continue }
            switch limit.mode {
            case .size:
                sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: item.fileExtension)
                if item.fileExtension != "mp3" {
                    sizeBytes += CacheManager.shared.fileSizeBytes(for: item.id, ext: "mp3")
                }
            case .duration:
                durationSeconds += item.duration ?? 0
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
        networkMonitor.cancel()
    }
}
