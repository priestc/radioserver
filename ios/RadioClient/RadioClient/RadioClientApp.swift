import SwiftUI

@main
struct RadioClientApp: App {
    @StateObject private var apiService = APIService.shared
    @StateObject private var audioPlayer = AudioPlayer.shared

    var body: some Scene {
        WindowGroup {
            TabView {
                NowPlayingView()
                    .tabItem {
                        Label("Now Playing", systemImage: "music.note")
                    }

                ChannelsView()
                    .tabItem {
                        Label("Channels", systemImage: "dot.radiowaves.left.and.right")
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
