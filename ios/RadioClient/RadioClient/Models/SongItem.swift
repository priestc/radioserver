import Foundation

struct SongItem: Codable, Identifiable, Equatable {
    let id: Int
    let title: String
    let artist: String
    let album: String?
    let albumId: Int?
    let year: Int?
    let duration: Double?

    enum CodingKeys: String, CodingKey {
        case id, title, artist, album
        case albumId = "album_id"
        case year, duration
    }
}

struct PlayedSong: Identifiable {
    let id = UUID()
    let song: SongItem
    let playedAt: Date
}

struct SyncResponse: Codable {
    let download: [SongItem]
}
