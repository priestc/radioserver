import SwiftUI

@main
struct RadioClientApp: App {
    @StateObject private var apiService = APIService.shared
    @StateObject private var audioPlayer = AudioPlayer.shared
    @Environment(\.scenePhase) private var scenePhase

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
                audioPlayer.fetchChannels()
                // No separate initial sync call here — scenePhase below reliably fires
                // for the initial launch too, and firing both was causing two concurrent
                // sync requests right at startup (confirmed in server logs), one of which
                // would get cancelled moments later by the next trigger.
            }
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                // Re-activate audio session in case iOS deactivated it while backgrounded
                audioPlayer.reactivateAudioSession()
                // Flush pendingPlayed immediately whenever the app becomes active
                // so plays recorded while offline are reported to the server promptly.
                // Covers both app launch and returning from background.
                audioPlayer.triggerSync(reason: "app foregrounded")
            }
        }
    }
}
