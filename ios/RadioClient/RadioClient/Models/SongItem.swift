import Foundation

struct SongItem: Codable, Identifiable, Equatable {
    let id: Int
    let title: String
    let artist: String
    let album: String?
    let albumId: Int?
    let year: Int?
    let duration: Double?
    let fileFormat: String?

    enum CodingKeys: String, CodingKey {
        case id, title, artist, album
        case albumId = "album_id"
        case year, duration
        case fileFormat = "file_format"
    }

    var fileExtension: String {
        (fileFormat ?? "mp3").lowercased()
    }
}

struct PlayedSong: Identifiable {
    let id = UUID()
    let song: SongItem
    let playedAt: Date
    let skipped: Bool
}

struct SyncResponse: Codable {
    let download: [SongItem]
}
