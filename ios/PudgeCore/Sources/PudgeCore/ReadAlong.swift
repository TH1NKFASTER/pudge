import Foundation

/// One sentence of the linked light novel; `start`/`end` are offsets on the
/// Mac reader's normalized character clock (no spaces or punctuation).
public struct ReadAlongSentence: Codable, Hashable {
    public let p: Int
    public let text: String
    public let start: Int
    public let end: Int
    /// Words: [utf16 start, utf16 end (in `text`), normalized offset, wordId, readingIndex].
    public let w: [[Int]]?

    public var words: [ReadAlongWord] {
        (w ?? []).compactMap { row in
            row.count >= 5 ? ReadAlongWord(start: row[0], end: row[1], offset: row[2], wordId: row[3], readingIndex: row[4]) : nil
        }
    }

    /// Word being spoken at a normalized offset (the last word that started).
    public func word(atOffset offset: Double) -> Int? {
        let items = words
        guard let first = items.first, Double(first.offset) <= offset else { return nil }
        return items.lastIndex { Double($0.offset) <= offset }
    }
}

public struct ReadAlongWord: Hashable {
    public let start: Int
    public let end: Int
    public let offset: Int
    public let wordId: Int
    public let readingIndex: Int
    public var token: StudyToken { StudyToken(start: start, end: end, wordId: wordId, readingIndex: readingIndex, state: 0) }
}

public struct ReadAlongChapter: Codable, Hashable, Identifiable {
    public let index: Int
    public let title: String
    public let startMs: Int64
    public let endMs: Int64
    public let sentences: [ReadAlongSentence]
    /// [audioMs, offset] pairs, both non-decreasing.
    public let anchors: [[Int64]]
    public var id: Int { index }

    /// Normalized offset at an audio time (linear between anchors).
    public func offset(atMs time: Int64) -> Double {
        guard let first = anchors.first, first.count == 2, let last = anchors.last, last.count == 2 else { return 0 }
        if time <= first[0] { return Double(first[1]) }
        if time >= last[0] { return Double(last[1]) }
        var low = 0, high = anchors.count - 1
        while high - low > 1 {
            let middle = (low + high) / 2
            if anchors[middle][0] <= time { low = middle } else { high = middle }
        }
        let left = anchors[low], right = anchors[high]
        let span = Double(max(1, right[0] - left[0]))
        return Double(left[1]) + Double(time - left[0]) / span * Double(right[1] - left[1])
    }

    /// Audio time where a normalized offset is spoken.
    public func timeMs(atOffset offset: Int) -> Int64 {
        guard let first = anchors.first, first.count == 2, let last = anchors.last, last.count == 2 else { return startMs }
        if Int64(offset) <= first[1] { return first[0] }
        if Int64(offset) >= last[1] { return last[0] }
        var low = 0, high = anchors.count - 1
        while high - low > 1 {
            let middle = (low + high) / 2
            if anchors[middle][1] < Int64(offset) { low = middle } else { high = middle }
        }
        let left = anchors[low], right = anchors[high]
        let span = Double(max(1, right[1] - left[1]))
        return left[0] + Int64(Double(Int64(offset) - left[1]) / span * Double(right[0] - left[0]))
    }

    /// Sentence being read at a normalized offset.
    public func sentence(atOffset offset: Double) -> Int? {
        guard !sentences.isEmpty else { return nil }
        var low = 0, high = sentences.count - 1
        while low < high {
            let middle = (low + high + 1) / 2
            if Double(sentences[middle].start) <= offset { low = middle } else { high = middle - 1 }
        }
        return low
    }

    /// Normalized offset of a reader position: paragraph `p` (illustrations not counted) and a
    /// UTF-16 offset in it. The paragraph's sentences concatenate to the paragraph text.
    public func offset(paragraph: Int, utf16 offset: Int) -> Int? {
        let items = sentences.filter { $0.p == paragraph }
        guard !items.isEmpty else { return nil }
        var cursor = 0
        for (index, sentence) in items.enumerated() {
            let length = sentence.text.utf16.count
            if offset < cursor + length || index == items.count - 1 {
                let local = max(0, offset - cursor)
                if let word = sentence.words.last(where: { $0.start <= local }) { return word.offset }
                return sentence.start
            }
            cursor += length
        }
        return nil
    }

    /// Consecutive sentence ranges that form one paragraph.
    public var paragraphRanges: [Range<Int>] {
        var result: [Range<Int>] = []
        var begin = 0
        for index in sentences.indices where index > 0 && sentences[index].p != sentences[index - 1].p {
            result.append(begin..<index); begin = index
        }
        if !sentences.isEmpty { result.append(begin..<sentences.count) }
        return result
    }
}

public struct ReadAlongDocument: Codable {
    public let schema: Int
    public let title: String
    public let chapters: [ReadAlongChapter]

    public static func load(_ url: URL) throws -> ReadAlongDocument {
        let value = try JSONDecoder().decode(ReadAlongDocument.self, from: Data(contentsOf: url))
        guard value.schema == 1, !value.chapters.isEmpty else { throw PudgeError.invalid("Unsupported read-along text") }
        return value
    }

    /// (chapter array index, sentence index) for an audio time.
    public func location(atMs time: Int64) -> (chapter: Int, sentence: Int)? {
        guard let chapter = chapters.lastIndex(where: { $0.startMs <= time }) ?? (chapters.isEmpty ? nil : 0) else { return nil }
        let value = chapters[chapter]
        guard let sentence = value.sentence(atOffset: value.offset(atMs: time)) else { return nil }
        return (chapter, sentence)
    }
}

/// The Mac's read-along clock: letters and digits count, spaces and punctuation are silent.
public enum ReadingClock {
    public static func isSpoken(_ scalar: Unicode.Scalar) -> Bool {
        if scalar.properties.isWhitespace { return false }
        switch scalar.properties.generalCategory {
        case .connectorPunctuation, .dashPunctuation, .closePunctuation, .finalPunctuation, .initialPunctuation,
             .otherPunctuation, .openPunctuation, .lineSeparator, .paragraphSeparator, .spaceSeparator:
            return false
        default:
            return true
        }
    }
    public static func spokenLength(_ text: String) -> Int {
        text.unicodeScalars.reduce(0) { $0 + (isSpoken($1) ? 1 : 0) }
    }
    /// Normalized offset where each token starts (tokens sorted by start), counted from `base`.
    public static func tokenOffsets(_ text: String, tokens: [StudyToken], base: Int) -> [Int] {
        let units = Array(text.utf16)
        var result: [Int] = []
        var cursor = 0, running = base
        for token in tokens {
            let at = min(max(cursor, token.start), units.count)
            if at > cursor { running += spokenLength(String(decoding: units[cursor..<at], as: UTF16.self)) }
            cursor = at
            result.append(running)
        }
        return result
    }
}

extension ReadAlongChapter {
    /// First normalized offset of a read-along paragraph.
    public func paragraphStart(_ paragraph: Int) -> Int? { sentences.first { $0.p == paragraph }?.start }
}
