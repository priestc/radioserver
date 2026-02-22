import SwiftUI

@main
struct RadioClientApp: App {
    @StateObject private var apiService = APIService()
    @StateObject private var audioPlayer = AudioPlayer()

    var body: some Scene {
        WindowGroup {
            TabView {
                NowPlayingView()
                    .tabItem {
                        Label("Now Playing", systemImage: "music.note")
                    }

                HistoryView()
                    .tabItem {
                        Label("History", systemImage: "clock")
                    }

                SettingsView()
                    .tabItem {
                        Label("Settings", systemImage: "gear")
                    }
            }
            .environmentObject(apiService)
            .environmentObject(audioPlayer)
            .onAppear {
                audioPlayer.apiService = apiService
                audioPlayer.startSyncTimer()
            }
        }
    }
}
