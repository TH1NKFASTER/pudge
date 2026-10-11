import Foundation

/// Word data from the last updates with the Mac, kept on the iPhone so a tapped word still
/// shows its meanings and words keep their colours while the Mac cannot be reached.
/// A complete dictionary entry is never replaced by an incomplete one.
public struct OfflineWords: Codable {
    public struct Entry: Codable {
        public var detail: WordDetail?
        public var state: StudyStateRow?
        /// When this entry last changed (seconds since 1970).
        public var savedAt: Double
    }

    public private(set) var entries: [String: Entry] = [:]
    /// Most words kept; the oldest entries leave first.
    public static let limit = 50_000

    public init() {}

    public mutating func remember(_ details: [WordDetail], at time: Double = Date().timeIntervalSince1970) {
        for detail in details where detail.wordId > 0 {
            var entry = entries[detail.id] ?? Entry(savedAt: time)
            if let previous = entry.detail, previous.complete, !detail.complete { continue }
            entry.detail = detail
            entry.savedAt = time
            entries[detail.id] = entry
        }
        prune()
    }

    public mutating func remember(states rows: [StudyStateRow], at time: Double = Date().timeIntervalSince1970) {
        for row in rows where row.wordId > 0 {
            var entry = entries[row.key] ?? Entry(savedAt: time)
            entry.state = row
            entry.savedAt = time
            entries[row.key] = entry
        }
        prune()
    }

    /// The remembered dictionary entry ("wordId:readingIndex").
    public func detail(_ key: String) -> WordDetail? { entries[key]?.detail }

    /// The last known knowledge state (newer than the entry's own when the word was graded since).
    public func state(_ key: String) -> StudyStateRow? { entries[key]?.state }

    /// Last known states saved after `time` (to colour words of an older download).
    public func states(newerThan time: Double) -> [String: StudyStateRow] {
        var result: [String: StudyStateRow] = [:]
        for (key, entry) in entries where entry.savedAt > time {
            if let state = entry.state { result[key] = state }
        }
        return result
    }

    /// [wordId, readingIndex] of words without a complete entry, each once; words still to
    /// learn come first (they are the ones tapped).
    public func missing(_ tokens: [StudyToken]) -> [[Int]] {
        var seen = Set<String>(), learn: [[Int]] = [], known: [[Int]] = []
        for token in tokens where token.hasEntry && seen.insert(token.key).inserted {
            if entries[token.key]?.detail?.complete == true { continue }
            if token.isMature || token.state == 4 { known.append([token.wordId, token.readingIndex]) }
            else { learn.append([token.wordId, token.readingIndex]) }
        }
        return learn + known
    }

    private mutating func prune() {
        guard entries.count > Self.limit else { return }
        let oldest = entries.sorted { $0.value.savedAt < $1.value.savedAt }.prefix(entries.count - Self.limit)
        for (key, _) in oldest { entries[key] = nil }
    }

    public static func load(_ url: URL) -> OfflineWords {
        guard let data = try? Data(contentsOf: url), let value = try? JSONDecoder().decode(OfflineWords.self, from: data) else { return OfflineWords() }
        return value
    }

    public func save(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(self).write(to: url, options: .atomic)
    }
}
