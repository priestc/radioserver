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

    /// The server measures ReplayGain in the background for songs that were never
    /// tagged, so a song can first arrive without a gain and come back on a later sync
    /// with one. Returns `items` with any such gains filled in from `fresh`, or nil if
    /// nothing changed.
    static func fillingInGains(_ items: [SongItem], from fresh: [SongItem]) -> [SongItem]? {
        let freshGains = Dictionary(
            fresh.compactMap { s in s.replaygainTrackGain.map { (s.id, $0) } },
            uniquingKeysWith: { a, _ in a }
        )
        guard !freshGains.isEmpty else { return nil }
        var changed = false
        let updated = items.map { item -> SongItem in
            guard item.replaygainTrackGain == nil, let gain = freshGains[item.id] else { return item }
            changed = true
            return SongItem(
                id: item.id, title: item.title, artist: item.artist, album: item.album,
                albumId: item.albumId, year: item.year, duration: item.duration,
                fileFormat: item.fileFormat, replaygainTrackGain: gain
            )
        }
        return changed ? updated : nil
    }
}

struct PlayedSong: Identifiable, Codable {
    let id: UUID
    let song: SongItem
    let playedAt: Date
    let skipped: Bool

    init(song: SongItem, playedAt: Date, skipped: Bool) {
        self.id = UUID()
        self.song = song
        self.playedAt = playedAt
        self.skipped = skipped
    }
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

