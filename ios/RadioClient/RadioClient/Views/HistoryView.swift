import SwiftUI

struct HistoryView: View {
    @EnvironmentObject var player: AudioPlayer

    var body: some View {
        NavigationStack {
            Group {
                if player.playHistory.isEmpty {
                    ContentUnavailableView(
                        "No History",
                        systemImage: "clock",
                        description: Text("Played songs will appear here.")
                    )
                } else {
                    List(player.playHistory) { entry in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(entry.song.title)
                                    .font(.body)
                                Text(entry.song.artist)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            Spacer()
                            Text(entry.playedAt, style: .time)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("History")
        }
    }
}
