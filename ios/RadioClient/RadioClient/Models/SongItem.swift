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
    let replaygainTrackGain: Double?

    enum CodingKeys: String, CodingKey {
        case id, title, artist, album
        case albumId = "album_id"
        case year, duration
        case fileFormat = "file_format"
        case replaygainTrackGain = "replaygain_track_gain"
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

struct Channel: Codable, Identifiable, Equatable {
    let id: Int
    let name: String
    let yearMin: Int?
    let yearMax: Int?
    let genreGroup: String?
    let genre: String?
    let artist: String?

    enum CodingKeys: String, CodingKey {
        case id, name, genre, artist
        case yearMin = "year_min"
        case yearMax = "year_max"
        case genreGroup = "genre_group"
    }

    var subtitle: String {
        var parts: [String] = []
        if let min = yearMin, let max = yearMax {
            parts.append("\(min)–\(max)")
        } else if let min = yearMin {
            parts.append("\(min) and newer")
        } else if let max = yearMax {
            parts.append("up to \(max)")
        }
        if let g = genreGroup { parts.append(g) }
        if let g = genre { parts.append(g) }
        if let a = artist { parts.append(a) }
        return parts.isEmpty ? "All music" : parts.joined(separator: " · ")
    }
}

struct ChannelsResponse: Codable {
    let channels: [Channel]
}
