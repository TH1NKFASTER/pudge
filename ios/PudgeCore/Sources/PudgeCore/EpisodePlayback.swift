import Foundation

// Episode list and player rules of the iPhone/iPad: one copy per episode number, the subtitle
// timing kept per episode (and shared with the Mac's mpv delay), and the OP/ED Skip button.

/// A detected opening ("intro") or ending ("outro") of an episode file, in seconds (the Mac's
/// intro skipper; GET media/anime `skipSegments`, absent from older Macs).
public struct SkipSegment: Codable, Hashable, Sendable {
    public let kind: String
    public let start: Double
    public let end: Double
    public init(kind: String, start: Double, end: Double) { self.kind = kind; self.start = start; self.end = end }
    public var isOpening: Bool { kind == "intro" }
}

/// The Skip opening / Skip ending button, as mpv's pudge_skip_segments.lua shows it: inside a
/// segment, during its first `buttonSeconds` (also after seeking back into it); on the phone also
/// whenever the controls are shown inside it. A tap seeks to the segment's end. No auto-skip
/// (the Mac has none either).
public enum SkipSegments {
    public static let buttonSeconds: Double = 5

    /// Segments mpv would accept: a known kind, 0 ≤ start < end, inside the file when its length is known.
    public static func valid(_ segments: [SkipSegment], durationMs: Int64) -> [SkipSegment] {
        let duration = Double(durationMs) / 1000
        return segments.filter { item in
            (item.kind == "intro" || item.kind == "outro") && item.start.isFinite && item.end.isFinite
                && item.start >= 0 && item.start < item.end && (duration <= 0 || item.end <= duration)
        }
    }

    /// The segment whose button is shown at `atMs` (nil: no button).
    public static func button(_ segments: [SkipSegment], atMs: Int64, durationMs: Int64, controlsShown: Bool = false) -> SkipSegment? {
        let time = Double(atMs) / 1000
        guard let segment = valid(segments, durationMs: durationMs).first(where: { time >= $0.start && time < $0.end }) else { return nil }
        return controlsShown || time - segment.start < buttonSeconds ? segment : nil
    }

    /// Where the button seeks to (ms).
    public static func targetMs(_ segment: SkipSegment) -> Int64 { Int64((segment.end * 1000).rounded()) }
}

/// The subtitle timing of each episode (ms, + shows the subtitles later), kept on the device and
/// sent to the Mac (its mpv delay for the same subtitle). A change made here and not sent yet
/// wins over the Mac's value; once sent, the Mac's value (set in mpv meanwhile) is taken.
public struct SubtitleDelays: Codable, Equatable, Sendable {
    public struct Entry: Codable, Equatable, Sendable {
        public var ms: Int
        public var unsent: Bool
    }

    public private(set) var entries: [String: Entry]

    public init(entries: [String: Entry] = [:]) { self.entries = entries }

    public func ms(_ id: Int) -> Int { entries[String(id)]?.ms ?? 0 }

    /// Set on this device; false when nothing changed.
    @discardableResult
    public mutating func set(_ ms: Int, for id: Int) -> Bool {
        let key = String(id)
        if let entry = entries[key] {
            guard entry.ms != ms else { return false }
        } else if ms == 0 {
            return false
        }
        entries[key] = Entry(ms: ms, unsent: true)
        return true
    }

    /// The Mac's value from its episode list (nil: an older Mac, nothing changes); false when nothing changed.
    @discardableResult
    public mutating func adopt(mac ms: Int?, for id: Int) -> Bool {
        guard let ms, entries[String(id)]?.unsent != true, self.ms(id) != ms else { return false }
        entries[String(id)] = ms == 0 ? nil : Entry(ms: ms, unsent: false)
        return true
    }

    /// The Mac has `ms` now (a newer change made meanwhile stays unsent).
    public mutating func sent(_ ms: Int, for id: Int) {
        guard let entry = entries[String(id)], entry.ms == ms else { return }
        entries[String(id)] = ms == 0 ? nil : Entry(ms: ms, unsent: false)
    }

    /// Changes waiting for the Mac, by episode id.
    public var unsent: [(id: Int, ms: Int)] {
        var result: [(id: Int, ms: Int)] = []
        for (key, entry) in entries where entry.unsent {
            if let id = Int(key) { result.append((id: id, ms: entry.ms)) }
        }
        return result.sorted { $0.id < $1.id }
    }
}

/// One item per episode number of a title. A Mac with two files of the same episode (an older
/// single download and a later batch) listed it twice, and both could be downloaded; a newer Mac
/// sends one, and the phone keeps one too (a copy kept on this device stays the one shown).
public enum EpisodeCopies {
    /// `items` in their order, without a second item of the same number; `rank` picks between
    /// copies (higher wins: downloaded here, then downloading), else the first stays.
    public static func unique(_ items: [EpisodeItem], rank: (EpisodeItem) -> Int = { _ in 0 }) -> [EpisodeItem] {
        var best: [Int: EpisodeItem] = [:]
        for item in items {
            if let kept = best[item.number], rank(kept) >= rank(item) { continue }
            best[item.number] = item
        }
        let chosen = Set(best.values.map(\.id))
        var seen = Set<Int>()
        return items.filter { chosen.contains($0.id) && seen.insert($0.id).inserted }
    }

    /// Another copy of the same title's episode among `others` (id, title's media id, number):
    /// its id, nil when there is none. Titles without a media id never match.
    public static func otherCopy(of item: EpisodeItem, among others: [(id: Int, mediaId: Int, number: Int)]) -> Int? {
        guard item.mediaId > 0 else { return nil }
        return others.first { $0.id != item.id && $0.mediaId == item.mediaId && $0.number == item.number }?.id
    }
}

/// A downloaded episode that the Mac counted as watched (in mpv, on another device): the Mac's
/// list stops offering an episode once it is watched (or the title's progress passed it), so the
/// phone listed its copy on its own with the old place, as "Continue ep 5", the next episode to
/// finish. Its title's progress on the Mac (the Mac's group or Home card) tells it was watched.
public enum MacWatchedEpisodes {
    /// `listed`: the Mac's list has the episode; `progress`: the title's watched count on the
    /// Mac (nil: the Mac does not list the title, nothing is known).
    public static func watchedOnMac(number: Int, listed: Bool, progress: Int?) -> Bool {
        guard !listed, number > 0, let progress, progress > 0 else { return false }
        return number <= progress
    }

    /// Whether the phone's copy takes the Mac's watch now: nothing of this device waits for the
    /// Mac (a place or a watch of its own), the player does not show it, and it was not taken before.
    public static func adopts(number: Int, listed: Bool, progress: Int?, watchedHere: Bool,
                              positionUnsent: Bool, playing: Bool) -> Bool {
        guard !watchedHere, !positionUnsent, !playing else { return false }
        return watchedOnMac(number: number, listed: listed, progress: progress)
    }
}
