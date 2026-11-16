import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var api: APIService
    @EnvironmentObject var audioPlayer: AudioPlayer
    @State private var testResult: String?
    @State private var isTesting = false
    @State private var showScanner = false
    @State private var cacheCleared = false
    @State private var confirmResetQueue = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("Local IP (e.g. 192.168.1.50)", text: $api.localURL)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)

                    TextField("Remote IP (e.g. 100.64.0.1)", text: $api.remoteURL)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)

                    HStack {
                        Text("Using")
                        Spacer()
                        Text(api.isOnLocalNetwork ? "Local" : "Remote")
                            .foregroundColor(.secondary)
                    }

                    Toggle("Download away from home", isOn: $api.allowRemoteDownloads)
                    Text("When off, new songs only download while connected to the local server. Cached music always plays.")
                        .font(.caption)
                        .foregroundColor(.secondary)

                    HStack {
                        TextField("API Key", text: $api.apiKey)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                            .fontDesign(.monospaced)

                        Button {
                            showScanner = true
                        } label: {
                            Image(systemName: "qrcode.viewfinder")
                                .font(.title2)
                        }
                    }
                }

                Section {
                    Button(action: testConnection) {
                        HStack {
                            Text("Test Connection")
                            Spacer()
                            if isTesting {
                                ProgressView()
                            }
                        }
                    }
                    .disabled(!api.isConfigured || isTesting)

                    if let result = testResult {
                        Text(result)
                            .font(.caption)
                            .foregroundColor(result.hasPrefix("Connected") ? .green : .red)
                    }
                }

                Section("Cache") {
                    ForEach(audioPlayer.cachedChannelStats, id: \.name) { entry in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(entry.name)
                                Spacer()
                                Text(CacheFormat.bytes(entry.sizeBytes))
                                    .foregroundColor(.secondary)
                            }
                            ChannelCacheLimitWidget(channelId: entry.channelId, channelName: entry.name)
                        }
                    }

                    let artworkSize = CacheManager.shared.totalArtworkSizeMB()
                    HStack {
                        Text("Artwork")
                        Spacer()
                        Text(String(format: "%.1f MB", artworkSize))
                            .foregroundColor(.secondary)
                    }

                    let totalAudio = CacheManager.shared.totalCacheSizeMB()
                    HStack {
                        Text("Total Cache")
                            .fontWeight(.semibold)
                        Spacer()
                        Text(String(format: "%.1f MB", totalAudio + artworkSize))
                            .foregroundColor(.secondary)
                            .fontWeight(.semibold)
                    }

                    Button {
                        audioPlayer.fillAllCaches()
                    } label: {
                        HStack {
                            Text("Fill All Caches")
                            Spacer()
                            if audioPlayer.isFillingCache {
                                ProgressView()
                            }
                        }
                    }
                    .disabled(audioPlayer.isFillingCache)

                    Button("Clear Cache", role: .destructive) {
                        audioPlayer.clearAllCaches()
                        cacheCleared = true
                    }
                    if cacheCleared {
                        Text("Cache cleared.")
                            .font(.caption)
                            .foregroundColor(.green)
                    }

                    Button(role: .destructive) {
                        confirmResetQueue = true
                    } label: {
                        HStack {
                            Text("Reset Queue")
                            Spacer()
                            if audioPlayer.isResettingQueue {
                                ProgressView()
                            }
                        }
                    }
                    .disabled(audioPlayer.isResettingQueue || !api.isConfigured)
                    .confirmationDialog(
                        "Reset the queue for every channel?",
                        isPresented: $confirmResetQueue,
                        titleVisibility: .visible
                    ) {
                        Button("Reset Queue", role: .destructive) {
                            audioPlayer.resetQueue()
                        }
                    } message: {
                        Text("Deletes every downloaded upcoming song, has the server pick new ones, then downloads them. The song playing now keeps playing.")
                    }
                    if let error = audioPlayer.resetQueueError {
                        Text("Couldn't reset the queue: \(error)")
                            .font(.caption)
                            .foregroundColor(.red)
                    }
                }
            }
            .onAppear {
                cacheCleared = false
                // Stats are computed on-device — no server round trip just for looking.
                audioPlayer.recalculateCacheStats()
            }
            // Pick up a newly entered server/key right away — otherwise nothing would
            // talk to the server until the next tab switch or app foreground.
            .onChange(of: api.localURL) { _, _ in audioPlayer.configurationChanged() }
            .onChange(of: api.remoteURL) { _, _ in audioPlayer.configurationChanged() }
            .onChange(of: api.allowRemoteDownloads) { _, _ in audioPlayer.configurationChanged() }
            .onChange(of: api.apiKey) { _, _ in audioPlayer.configurationChanged() }
            .scrollDismissesKeyboard(.interactively)
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") {
                        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                    }
                }
            }
            .navigationTitle("Settings")
            .sheet(isPresented: $showScanner) {
                QRScannerView { code in
                    api.apiKey = code
                }
            }
        }
    }

    private func testConnection() {
        isTesting = true
        testResult = nil
        Task {
            let which = api.isOnLocalNetwork ? "local" : "remote"
            let result = await api.testConnection()
            await MainActor.run {
                switch result {
                case .success(let count):
                    testResult = "Connected via \(which) — \(count) songs available"
                case .failure(let error):
                    testResult = "Failed via \(which): \(error.localizedDescription)"
                }
                isTesting = false
            }
        }
    }
}
