import Foundation

/// The audiobook's controls outside the app (lock screen, Control Center, headphones, Apple Watch):
/// chapter jumps, the chapter's progress in Now Playing, speeds and skip intervals.
public enum NowPlayingChapters {
    /// Previous chapter restarts the current one when more than this is played of it.
    public static let restartThresholdMs: Int64 = 3000
    /// The app's ±skip (the player's 15 s buttons).
    public static let skipSeconds: Int64 = 15
    /// Every speed the app's speed menus offer (the player's and the reader's), ascending.
    public static let rates: [Float] = [0.5, 0.75, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3]

    /// Chapters in order with a positive length.
    static func ordered(_ chapters: [AudioChapter]) -> [AudioChapter] {
        chapters.filter { $0.endMs > $0.startMs }.sorted { $0.startMs < $1.startMs }
    }

    /// Index (in `ordered`) of the chapter playing at `positionMs`; nil before the first one.
    static func currentIndex(_ positionMs: Int64, _ chapters: [AudioChapter]) -> Int? {
        chapters.lastIndex { $0.startMs <= positionMs }
    }

    /// Where Next chapter goes: the next chapter's start; nil on the last chapter (or without chapters).
    public static func nextStart(positionMs: Int64, chapters: [AudioChapter]) -> Int64? {
        ordered(chapters).first { $0.startMs > positionMs }?.startMs
    }

    /// Where Previous chapter goes: the start of the current chapter when more than 3 s of it is
    /// played, else the previous chapter's start (the book's start before the first chapter and
    /// without chapters).
    public static func previousStart(positionMs: Int64, chapters: [AudioChapter]) -> Int64 {
        let list = ordered(chapters)
        guard let index = currentIndex(positionMs, list) else { return 0 }
        let start = list[index].startMs
        if positionMs - start > restartThresholdMs { return start }
        return index > 0 ? list[index - 1].startMs : 0
    }

    /// The chapter's progress in Now Playing: elapsed and length of the chapter at `positionMs`
    /// (the whole book without chapters or between them), its number (1-based) and the count.
    public struct Progress: Equatable, Sendable {
        public let startMs: Int64
        public let elapsedMs: Int64
        public let durationMs: Int64
        public let number: Int?
        public let count: Int
    }

    public static func progress(positionMs: Int64, durationMs: Int64, chapters: [AudioChapter]) -> Progress {
        let list = ordered(chapters)
        let position = min(max(0, positionMs), max(0, durationMs))
        if let index = currentIndex(position, list), position < list[index].endMs {
            let chapter = list[index]
            let end = min(chapter.endMs, max(durationMs, chapter.startMs))
            return Progress(startMs: chapter.startMs, elapsedMs: position - chapter.startMs,
                            durationMs: max(0, end - chapter.startMs), number: index + 1, count: list.count)
        }
        return Progress(startMs: 0, elapsedMs: position, durationMs: max(0, durationMs), number: nil, count: list.count)
    }

    /// A scrub on the lock screen / Watch (chapter time, as Now Playing shows it) as a book position.
    public static func scrubTarget(seconds: Double, positionMs: Int64, durationMs: Int64, chapters: [AudioChapter]) -> Int64 {
        let shown = progress(positionMs: positionMs, durationMs: durationMs, chapters: chapters)
        let offset = seconds.isFinite ? Int64((max(0, seconds) * 1000).rounded()) : 0
        return min(max(0, durationMs), shown.startMs + min(offset, shown.durationMs))
    }

    /// A speed asked from outside the app as one the app offers (the nearest).
    public static func supportedRate(_ rate: Float) -> Float {
        guard rate.isFinite else { return 1 }
        return rates.min { abs($0 - rate) < abs($1 - rate) } ?? 1
    }
}
