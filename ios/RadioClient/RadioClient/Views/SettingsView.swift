import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var api: APIService
    @EnvironmentObject var audioPlayer: AudioPlayer
    @State private var testResult: String?
    @State private var isTesting = false
    @State private var showScanner = false

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
                    HStack {
                        Text("Buffer Size (MB)")
                        Spacer()
                        TextField("MB", value: $api.bufferCacheMB, format: .number)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 80)
                    }

                    let channelCaches = audioPlayer.cacheSizeMBPerChannel()
                    ForEach(channelCaches, id: \.name) { entry in
                        HStack {
                            Text(entry.name)
                            Spacer()
                            Text(String(format: "%.1f MB", entry.sizeMB))
                                .foregroundColor(.secondary)
                        }
                    }

                    let artworkSize = CacheManager.shared.totalArtworkSizeMB()
                    HStack {
                        Text("Artwork")
                        Spacer()
                        Text(String(format: "%.1f MB", artworkSize))
                            .foregroundColor(.secondary)
                    }

                    let totalAudio = channelCaches.reduce(0.0) { $0 + $1.sizeMB }
                    HStack {
                        Text("Total Cache")
                            .fontWeight(.semibold)
                        Spacer()
                        Text(String(format: "%.1f MB", totalAudio + artworkSize))
                            .foregroundColor(.secondary)
                            .fontWeight(.semibold)
                    }

                    Button("Clear Cache", role: .destructive) {
                        CacheManager.shared.clearCache()
                    }
                }
            }
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
