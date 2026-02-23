import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var api: APIService
    @State private var testResult: String?
    @State private var isTesting = false
    @State private var showScanner = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("Server address (e.g. 192.168.1.50:8000)", text: $api.serverURL)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)

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

                    let audioSize = CacheManager.shared.totalCacheSizeMB()
                    HStack {
                        Text("Audio Cache")
                        Spacer()
                        Text(String(format: "%.1f MB", audioSize))
                            .foregroundColor(.secondary)
                    }

                    let artworkSize = CacheManager.shared.totalArtworkSizeMB()
                    HStack {
                        Text("Artwork Cache")
                        Spacer()
                        Text(String(format: "%.1f MB", artworkSize))
                            .foregroundColor(.secondary)
                    }

                    Button("Clear Cache", role: .destructive) {
                        CacheManager.shared.clearCache()
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
            let result = await api.testConnection()
            await MainActor.run {
                switch result {
                case .success(let count):
                    testResult = "Connected — \(count) songs available"
                case .failure(let error):
                    testResult = "Failed: \(error.localizedDescription)"
                }
                isTesting = false
            }
        }
    }
}
