import Foundation

/// The end of an anime watched on this device: the Mac's score wheel and its next entry
/// (`pudge/iphone_finale.py`). A counted watch of the final episode (a movie or a one-episode title:
/// its only one) answers `finale`; the phone shows the wheel once the player closed. Being caught up
/// with an airing title asks nothing (as on the Mac).

/// `kind` "final" (an older Mac also sent "latest" for an airing title caught up: not offered);
/// `episode`: the rating episode (the last one); `prompted`: answered already (on the Mac or a phone).
public struct AnimeFinale: Codable, Hashable, Identifiable, Sendable {
    public let mediaId: Int
    public let title: String
    public let kind: String
    public let episode: Int
    public var userScore: Double?
    public var prompted: Bool
    public var id: String { "\(mediaId):\(episode)" }
    public var isFinal: Bool { kind == "final" }
    /// Only the end of the anime shows the wheel.
    public var offersWheel: Bool { isFinal && episode > 0 }
    public init(mediaId: Int, title: String, kind: String = "final", episode: Int, userScore: Double? = nil, prompted: Bool = false) {
        self.mediaId = mediaId; self.title = title; self.kind = kind; self.episode = episode
        self.userScore = userScore; self.prompted = prompted
    }
    enum CodingKeys: String, CodingKey { case mediaId, title, kind, episode, userScore, prompted }
    public init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        mediaId = try row.decode(Int.self, forKey: .mediaId)
        title = (try? row.decodeIfPresent(String.self, forKey: .title)) ?? ""
        kind = (try? row.decodeIfPresent(String.self, forKey: .kind)) ?? "final"
        episode = (try? row.decodeIfPresent(Int.self, forKey: .episode)) ?? 0
        userScore = try? row.decodeIfPresent(Double.self, forKey: .userScore)
        prompted = (try? row.decodeIfPresent(Bool.self, forKey: .prompted)) ?? false
    }
}

public enum AnimeFinaleRule {
    /// Without the Mac's answer (away, or an older Mac): the final episode by the list's count, a
    /// movie or a one-episode title.
    public static func local(mediaId: Int, title: String, number: Int, total: Int?, movie: Bool?) -> AnimeFinale? {
        guard mediaId > 0, number >= 0 else { return nil }
        if let total, total > 0, number >= total { return AnimeFinale(mediaId: mediaId, title: title, episode: total) }
        if (total == 1 || movie == true), number <= 1 { return AnimeFinale(mediaId: mediaId, title: title, episode: 1) }
        return nil
    }
}

/// The Mac's score wheel (index.html SCORE_WHEEL_WEIGHTS): scores 1–10, the middle ones likelier;
/// angles in degrees clockwise from the top (the pointer).
public enum ScoreWheel {
    public static let weights = [1, 2, 4, 7, 10, 10, 7, 4, 2, 1]
    public static var total: Int { weights.reduce(0, +) }

    public struct Segment: Hashable, Sendable {
        public let score: Int
        public let start: Double
        public let end: Double
        public var center: Double { (start + end) / 2 }
    }

    public static var segments: [Segment] {
        var offset = 0
        return weights.enumerated().map { index, weight in
            let start = Double(offset) / Double(total) * 360
            offset += weight
            return Segment(score: index + 1, start: start, end: Double(offset) / Double(total) * 360)
        }
    }

    /// The score a draw in [0, 1) lands on.
    public static func score(_ draw: Double) -> Int {
        var pick = min(max(0, draw), 0.999_999) * Double(total)
        for (index, weight) in weights.enumerated() {
            pick -= Double(weight)
            if pick < 0 { return index + 1 }
        }
        return weights.count
    }

    /// A spin: never the same score twice in a row (as on the Mac); `target` is the angle within
    /// its segment that stops under the pointer. `random` gives values in [0, 1).
    public static func spin(previous: Int?, random: () -> Double) -> (score: Int, target: Double) {
        var result = score(random())
        var tries = 0
        while result == previous, tries < 64 { result = score(random()); tries += 1 }
        if result == previous { result = result == 5 ? 6 : 5 }
        let segment = segments[result - 1]
        return (result, segment.start + random() * (segment.end - segment.start))
    }

    /// The wheel's next rotation: `turns` full turns on from `current`, ending with `target` under
    /// the pointer (the Mac's `current + turns*360 - normalized - target`).
    public static func rotation(from current: Double, turns: Int, target: Double) -> Double {
        let normalized = (current.truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360)
        return current + Double(turns) * 360 - normalized - target
    }

    /// Seconds of a spin: the first one of a wheel display runs a second longer (it looked instant),
    /// Spin again keeps the usual length. A flick on the wheel may lengthen it a little by its
    /// speed (points per second), capped: the score is the same weighted draw as the button's.
    public static let spinSeconds = 1.0
    public static let firstSpinExtraSeconds = 1.0
    public static let flickExtraMaxSeconds = 0.4
    public static func spinDuration(spinsBefore: Int, flickSpeed: Double? = nil) -> Double {
        var seconds = spinSeconds + (spinsBefore > 0 ? 0 : firstSpinExtraSeconds)
        if let flickSpeed, flickSpeed.isFinite {
            seconds += min(flickExtraMaxSeconds, abs(flickSpeed) / 3000 * flickExtraMaxSeconds)
        }
        return seconds
    }

    /// A drag on the wheel that spins it: long and fast enough to be a swipe, not a tap or a rest.
    public static func isFlick(distance: Double, speed: Double) -> Bool {
        distance.isFinite && speed.isFinite && abs(distance) >= 24 && abs(speed) >= 150
    }

    /// The score under the pointer for a rotation (what the wheel shows).
    public static func scoreAt(rotation: Double) -> Int {
        let angle = ((-rotation).truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360)
        return segments.first { angle >= $0.start && angle < $0.end }?.score ?? weights.count
    }
}

/// The finale sheet's height: its content plus the navigation bar and the bottom inset, so no empty
/// area stays below (a tall form sheet left ~40 % empty); within `minimum`…`maximum`.
public enum FinaleSheetLayout {
    public static let chrome = 72.0
    public static let minimum = 320.0
    public static let maximum = 900.0
    public static let estimate = 560.0
    public static func height(content: Double) -> Double {
        guard content.isFinite, content > 0 else { return estimate }
        return min(maximum, max(minimum, (content + chrome).rounded(.up)))
    }
}

/// A score chosen (or Skip) waiting for the Mac; `score` nil: skipped.
public struct FinaleAnswer: Codable, Hashable, Sendable {
    public let mediaId: Int
    public let episode: Int
    public let score: Double?
    public let at: Double
    public init(mediaId: Int, episode: Int, score: Double?, at: Double) {
        self.mediaId = mediaId; self.episode = episode; self.score = score; self.at = at
    }
}

/// The answers of this device: those the Mac has not got yet (sent once it answers, oldest first)
/// and the finales answered here (not offered again).
public struct FinaleQueue: Codable, Hashable, Sendable {
    public private(set) var pending: [FinaleAnswer] = []
    public private(set) var answered: [String] = []
    public static let keep = 200
    public init() {}

    public func offers(_ finale: AnimeFinale) -> Bool { !finale.prompted && !answered.contains(finale.id) }

    /// A score (or Skip, nil) for `finale`: the latest answer for a title replaces one not sent yet.
    public mutating func answer(_ finale: AnimeFinale, score: Double?, at: Double) {
        pending.removeAll { $0.mediaId == finale.mediaId }
        pending.append(FinaleAnswer(mediaId: finale.mediaId, episode: finale.episode, score: score.map { min(10, max(1, $0)) }, at: at))
        answered.removeAll { $0 == finale.id }
        answered.append(finale.id)
        if answered.count > Self.keep { answered.removeFirst(answered.count - Self.keep) }
    }

    /// The Mac took it (or refused it for good): it goes, unless a newer one replaced it meanwhile.
    public mutating func sent(_ answer: FinaleAnswer) {
        pending.removeAll { $0 == answer }
    }
}

/// `POST media/anime/<id>/score`.
public struct FinaleScoreRequest: Codable, Sendable {
    public let score: Double?
    public let skip: Bool?
    public let episode: Int
    public init(_ answer: FinaleAnswer) {
        score = answer.score; skip = answer.score == nil ? true : nil; episode = answer.episode
    }
}

public struct FinaleScoreReceipt: Decodable, Sendable {
    public let ok: Bool?
    public let score: Double?
    public let anilistPending: Bool?
    public let skipped: Bool?
}

/// The Mac's next entry (its direct sequel), `GET media/anime/<id>/next-entry`.
public struct FinaleSequel: Codable, Hashable, Sendable {
    public let mediaId: Int
    public let title: String
    public let siteUrl: String?
    public let startDate: String?
    public let mediaStatus: String?
    public let listStatus: String?
    public let format: String?
    public let episodes: Int?
    public let released: Bool?
    public let canPlanning: Bool?
    public let canWatching: Bool?
    public let canDownload: Bool?
    public init(mediaId: Int, title: String, siteUrl: String? = nil, startDate: String? = nil, mediaStatus: String? = nil,
                listStatus: String? = nil, format: String? = nil, episodes: Int? = nil, released: Bool? = nil,
                canPlanning: Bool? = nil, canWatching: Bool? = nil, canDownload: Bool? = nil) {
        self.mediaId = mediaId; self.title = title; self.siteUrl = siteUrl; self.startDate = startDate
        self.mediaStatus = mediaStatus; self.listStatus = listStatus; self.format = format; self.episodes = episodes
        self.released = released; self.canPlanning = canPlanning; self.canWatching = canWatching; self.canDownload = canDownload
    }

    /// The Mac's wording of where it stands (finalSequelStatusText).
    public var statusText: String {
        switch (listStatus ?? "").uppercased() {
        case "CURRENT": return "Already watching"
        case "COMPLETED": return "Already completed"
        case "PLANNING": return "In Planning"
        case "REPEATING": return "Rewatching"
        default: break
        }
        switch (mediaStatus ?? "").uppercased() {
        case "NOT_YET_RELEASED": return "Not yet released"
        case "RELEASING": return "Airing"
        case "CANCELLED": return "Cancelled"
        default: return released == false ? "Not yet released" : "Released"
        }
    }
}

public struct FinaleSequelAnswer: Decodable, Sendable {
    public let sequel: FinaleSequel?
}

/// The Mac's next entries fetched ahead (when the final episode's player opened), by title, for a
/// short while: the finale sheet shows a fresh one at once instead of "Checking AniList…".
/// `nil` sequel kept too (the Mac has none): `.hit(nil)`.
public struct FinaleSequelCache: Sendable {
    public enum Lookup: Equatable, Sendable {
        case miss
        case hit(FinaleSequel?)
    }
    /// Seconds an answer serves (an episode lasts ~24 min; the Mac keeps its own for hours).
    public static let ttl = 45.0 * 60
    public static let keep = 20
    private struct Entry: Sendable { let sequel: FinaleSequel?; let at: Double }
    private var entries: [Int: Entry] = [:]
    public let ttl: Double
    public init(ttl: Double = FinaleSequelCache.ttl) { self.ttl = ttl }

    public func lookup(_ mediaId: Int, now: Double) -> Lookup {
        guard let entry = entries[mediaId], now >= entry.at, now - entry.at < ttl else { return .miss }
        return .hit(entry.sequel)
    }

    public func fresh(_ mediaId: Int, now: Double) -> Bool { lookup(mediaId, now: now) != .miss }

    public mutating func store(_ mediaId: Int, sequel: FinaleSequel?, at: Double) {
        guard mediaId > 0 else { return }
        entries[mediaId] = Entry(sequel: sequel, at: at)
        entries = entries.filter { at - $0.value.at < ttl }
        if entries.count > Self.keep {
            let newest = entries.sorted { $0.value.at > $1.value.at }.prefix(Self.keep)
            entries = Dictionary(uniqueKeysWithValues: newest.map { ($0.key, $0.value) })
        }
    }

    public mutating func forget(_ mediaId: Int) { entries[mediaId] = nil }
}

/// `POST media/anime/<id>/next-entry {action}`.
public struct FinaleActionRequest: Codable, Sendable {
    public let action: String
    public init(_ action: String) { self.action = action }
}

public struct FinaleActionAnswer: Decodable, Sendable {
    public let ok: Bool?
    public let reason: String?
    public let anilistPending: Bool?
    public let background: Bool?
    public let release: String?
    public let sequel: FinaleSequel?

    /// What the sheet says after the action (the Mac's own messages, in English).
    public func message(_ action: String) -> String {
        if ok == true {
            switch action {
            case "planning": return anilistPending == true ? "Added to Planning (AniList is updated later)." : "Added to Planning."
            case "watching": return anilistPending == true ? "Moved to Watching (AniList is updated later)." : "Moved to Watching."
            default:
                if background == true { return "The Mac searches for its released episodes and downloads them." }
                return "Download added" + ((release ?? "").isEmpty ? "." : ": \(release ?? "")")
            }
        }
        switch reason ?? "" {
        case "no_release": return "No suitable torrent yet. The Mac keeps looking."
        case "not_released": return "It has not been released yet."
        case "downloads_disabled": return "Torrent downloads are turned off on the Mac."
        case "download_busy": return "The Mac is busy with another search or download. Try again in a moment."
        case "no_sequel": return "The Mac has no next entry for this anime."
        default: return "The Mac could not do it."
        }
    }
}

/// `POST media/episodes/<id>/watched`: StudyActionResult's fields plus `finale`.
public struct EpisodeWatchedAnswer: Decodable, Sendable {
    public let ok: Bool
    public let outcome: String
    public let message: String
    public let finale: AnimeFinale?
    enum CodingKeys: String, CodingKey { case ok, outcome, message, finale }
    public init(ok: Bool, outcome: String, message: String = "", finale: AnimeFinale? = nil) {
        self.ok = ok; self.outcome = outcome; self.message = message; self.finale = finale
    }
    public init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        ok = try row.decode(Bool.self, forKey: .ok)
        outcome = (try? row.decodeIfPresent(String.self, forKey: .outcome)) ?? ""
        message = (try? row.decodeIfPresent(String.self, forKey: .message)) ?? ""
        // A finale the phone cannot read never fails the watched mark.
        finale = try? row.decodeIfPresent(AnimeFinale.self, forKey: .finale)
    }
}

/// A finale answer (score) the Mac refused, by HTTP status: dropped only when it is refused for
/// good (no such route on an older Mac, the title gone, the value invalid); auth, conflicts and
/// rate limits are sent again later.
public enum FinaleRefusal {
    public static func final(_ status: Int) -> Bool { [404, 405, 410, 422].contains(status) }
}
