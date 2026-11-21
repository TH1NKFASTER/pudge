import Foundation

/// A place in text between two UTF-16 units of a paragraph (offset 0 = before its first
/// character). `paragraph` is the paragraph's index in the reader's chapter, 0 for a text of its
/// own (a subtitle line, the word card).
public struct TextPosition: Hashable, Comparable {
    public var paragraph: Int
    public var offset: Int
    public init(paragraph: Int, offset: Int) { self.paragraph = paragraph; self.offset = offset }
    public static func < (left: TextPosition, right: TextPosition) -> Bool {
        left.paragraph != right.paragraph ? left.paragraph < right.paragraph : left.offset < right.offset
    }
}

/// Which end of a selection a handle moves.
public enum TextSelectionHandle: Hashable { case start, end }

/// A word of the reader by paragraph and token index (Look Up).
public struct TextWord: Hashable {
    public let paragraph: Int
    public let token: Int
    public init(paragraph: Int, token: Int) { self.paragraph = paragraph; self.token = token }
}

/// The selection menu's items, in their order.
public enum TextSelectionAction: Hashable {
    case copy
    case lookUp(TextWord)
    /// Explain Grammar: the selected text, with the Mac's LLM (GrammarChat).
    case explain
    case listen(TextPosition)
    case markHere(TextPosition)
}

/// Selected text from `start` up to `end` (not included), in one paragraph or over several.
/// The reader keeps it for the whole chapter (paragraph + UTF-16 offset for both ends): its
/// paragraph views come and go while it scrolls, each one draws its own part.
public struct TextSelectionRange: Hashable {
    public let start: TextPosition
    public let end: TextPosition

    /// Either order: the earlier place becomes the start.
    public init(_ one: TextPosition, _ other: TextPosition) {
        if other < one { start = other; end = one } else { start = one; end = other }
    }

    /// A UTF-16 range of one paragraph.
    public init(paragraph: Int, range: Range<Int>) {
        self.init(TextPosition(paragraph: paragraph, offset: range.lowerBound),
                  TextPosition(paragraph: paragraph, offset: range.upperBound))
    }

    public var isEmpty: Bool { start == end }

    /// The place a handle holds.
    public func position(of handle: TextSelectionHandle) -> TextPosition { handle == .start ? start : end }

    public func contains(paragraph index: Int) -> Bool { index >= start.paragraph && index <= end.paragraph }

    /// The selected UTF-16 range of one paragraph `length` units long; nil when none of it is selected.
    public func part(ofParagraph index: Int, length: Int) -> Range<Int>? {
        guard contains(paragraph: index) else { return nil }
        let size = max(0, length)
        let low = index == start.paragraph ? min(max(0, start.offset), size) : 0
        let high = index == end.paragraph ? min(max(0, end.offset), size) : size
        return high > low ? low..<high : nil
    }

    /// Both selections and everything between them (a long press dragged over more words).
    public func union(_ other: TextSelectionRange) -> TextSelectionRange {
        TextSelectionRange(min(start, other.start), max(end, other.end))
    }

    /// `handle` dragged to `position`; the other end stays where it is. Dragged past the other
    /// end the two swap (the returned handle is the one under the finger). The other end's own
    /// place changes nothing: a selection never becomes empty.
    public func moving(_ handle: TextSelectionHandle, to position: TextPosition) -> (range: TextSelectionRange, handle: TextSelectionHandle) {
        let fixed = handle == .start ? end : start
        guard position != fixed else { return (self, handle) }
        return (TextSelectionRange(fixed, position), position < fixed ? .start : .end)
    }

    /// Offsets kept inside their paragraphs and ends on a paragraph's edge moved into what they
    /// select: a start after a paragraph's last character goes to the start of the next
    /// paragraph, an end before a paragraph's first character to the end of the previous one,
    /// past illustrations. `length(index)`: a paragraph's UTF-16 length (0 for an illustration
    /// or a paragraph that does not exist). nil when nothing is selected then.
    public func normalized(length: (Int) -> Int) -> TextSelectionRange? {
        var low = TextPosition(paragraph: start.paragraph, offset: min(max(0, start.offset), max(0, length(start.paragraph))))
        var high = TextPosition(paragraph: end.paragraph, offset: min(max(0, end.offset), max(0, length(end.paragraph))))
        while low < high, low.offset >= length(low.paragraph) {
            low = TextPosition(paragraph: low.paragraph + 1, offset: 0)
        }
        while low < high, high.offset <= 0 {
            let previous = high.paragraph - 1
            high = TextPosition(paragraph: previous, offset: max(0, length(previous)))
        }
        return low < high ? TextSelectionRange(low, high) : nil
    }

    /// The selected text (Copy): paragraphs joined by line breaks, illustrations left out.
    public func text(_ paragraph: (Int) -> TextParagraph?) -> String {
        var parts: [String] = []
        for index in start.paragraph...end.paragraph {
            guard let item = paragraph(index), !item.image else { continue }
            let units = Array(item.text.utf16)
            guard let selected = part(ofParagraph: index, length: units.count) else { continue }
            parts.append(String(decoding: units[selected], as: UTF16.self))
        }
        return parts.joined(separator: "\n")
    }

    /// The first word with a dictionary entry in the selection (Look Up).
    public func firstWord(_ paragraph: (Int) -> TextParagraph?) -> TextWord? {
        for index in start.paragraph...end.paragraph {
            guard let item = paragraph(index), !item.image,
                  let selected = part(ofParagraph: index, length: item.text.utf16.count) else { continue }
            if let token = item.tokens.firstIndex(where: { $0.hasEntry && $0.start < selected.upperBound && $0.end > selected.lowerBound }) {
                return TextWord(paragraph: index, token: token)
            }
        }
        return nil
    }

    /// Whether one paragraph (`length` UTF-16 units) looks different under two selections: its
    /// selected part, or a handle in it, moved. Only those paragraphs redraw.
    public static func changes(_ old: TextSelectionRange?, _ new: TextSelectionRange?, paragraph index: Int, length: Int) -> Bool {
        func look(_ value: TextSelectionRange?) -> [Int?] {
            guard let value else { return [nil, nil, nil, nil] }
            let selected = value.part(ofParagraph: index, length: length)
            return [selected?.lowerBound, selected?.upperBound,
                    value.start.paragraph == index ? value.start.offset : nil,
                    value.end.paragraph == index ? value.end.offset : nil]
        }
        return look(old) != look(new)
    }

    /// The selection menu: Copy, Look Up (the first word with an entry), Explain Grammar (the
    /// reader), then Listen from here and Read up to here at the selection's start when the
    /// screen offers them.
    public func actions(_ paragraph: (Int) -> TextParagraph?, listen: Bool, markHere: Bool, explain: Bool = false) -> [TextSelectionAction] {
        var result: [TextSelectionAction] = [.copy]
        if let word = firstWord(paragraph) { result.append(.lookUp(word)) }
        if explain, !text(paragraph).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { result.append(.explain) }
        if listen { result.append(.listen(start)) }
        if markHere { result.append(.markHere(start)) }
        return result
    }
}

/// What a long press, two taps and three taps select in a paragraph (UTF-16 ranges).
public enum TextUnits {
    /// Three taps: the sentence (the reader) or the whole text (a subtitle line).
    public enum Triple: Hashable { case sentence, paragraph }

    /// A long press starts with, and grows by, the word under the finger, or one character
    /// outside words.
    public static func unit(_ paragraph: TextParagraph, at offset: Int) -> Range<Int>? {
        if let word = token(paragraph, at: offset) { return word }
        return character(paragraph.text, at: offset)
    }

    /// Two taps select the word, three taps the sentence or the whole text.
    public static func taps(_ count: Int, in paragraph: TextParagraph, at offset: Int, triple: Triple) -> Range<Int>? {
        guard count >= 3 else { return word(paragraph, at: offset) }
        return triple == .sentence ? sentence(paragraph.text, at: offset) : whole(paragraph.text)
    }

    /// The word at an offset: its token, or outside words the run of characters of one kind
    /// around it (kanji, hiragana, katakana, letters and digits, spaces, other signs).
    public static func word(_ paragraph: TextParagraph, at offset: Int) -> Range<Int>? {
        if let found = token(paragraph, at: offset) { return found }
        let pieces = characters(paragraph.text)
        guard let at = pieces.firstIndex(where: { $0.range.contains(offset) }) else { return nil }
        let kind = pieces[at].kind
        func outsideWords(_ index: Int) -> Bool { paragraph.token(at: pieces[index].range.lowerBound) == nil }
        var low = at, high = at
        while low > 0, pieces[low - 1].kind == kind, outsideWords(low - 1) { low -= 1 }
        while high + 1 < pieces.count, pieces[high + 1].kind == kind, outsideWords(high + 1) { high += 1 }
        return pieces[low].range.lowerBound..<pieces[high].range.upperBound
    }

    /// One character at an offset, with the combining marks that belong to it.
    public static func character(_ text: String, at offset: Int) -> Range<Int>? {
        characters(text).first { $0.range.contains(offset) }?.range
    }

    /// The sentence around an offset, without the spaces around it. StudyText.sentence's stops
    /// end a sentence (。！？!?… and line breaks); stops and closing brackets right after a stop
    /// belong to the sentence it ends.
    public static func sentence(_ text: String, at offset: Int) -> Range<Int>? {
        let units = Array(text.utf16)
        guard !units.isEmpty else { return nil }
        let at = min(max(0, offset), units.count - 1)
        var low = 0, high = units.count, index = 0
        while index < units.count {
            guard stops.contains(units[index]) else { index += 1; continue }
            var next = index + 1
            while next < units.count, stops.contains(units[next]) || closers.contains(units[next]) { next += 1 }
            if next <= at { low = next } else { high = next; break }
            index = next
        }
        return trimmed(units, low..<high)
    }

    /// The whole text without the spaces around it.
    public static func whole(_ text: String) -> Range<Int>? {
        let units = Array(text.utf16)
        return trimmed(units, 0..<units.count)
    }

    private static let stops = Set("。！？!?…\n".utf16)
    private static let closers = Set("」』）)］]｝}〉》】〕〗〙〛〟”’\"'»".utf16)
    private static let spaces = Set(" \t\r\n\u{3000}".utf16)

    private static func token(_ paragraph: TextParagraph, at offset: Int) -> Range<Int>? {
        let length = paragraph.text.utf16.count
        guard offset >= 0, offset < length, let index = paragraph.token(at: offset) else { return nil }
        let item = paragraph.tokens[index]
        let low = max(0, item.start), high = min(length, item.end)
        return high > low ? low..<high : nil
    }

    private static func trimmed(_ units: [UInt16], _ bounds: Range<Int>) -> Range<Int>? {
        var low = bounds.lowerBound, high = bounds.upperBound
        while low < high, spaces.contains(units[low]) { low += 1 }
        while high > low, spaces.contains(units[high - 1]) { high -= 1 }
        return high > low ? low..<high : nil
    }

    private enum Kind { case kanji, hiragana, katakana, letters, space, other }

    /// A text's characters with their UTF-16 ranges and kinds. Combining marks, variation
    /// selectors and joiners are part of the character before them; ー, ゛ and ゜ take its kind.
    private static func characters(_ text: String) -> [(range: Range<Int>, kind: Kind)] {
        var result: [(range: Range<Int>, kind: Kind)] = []
        var offset = 0
        for scalar in text.unicodeScalars {
            let next = offset + UTF16.width(scalar)
            if let last = result.last, joins(scalar) {
                result[result.count - 1] = (range: last.range.lowerBound..<next, kind: last.kind)
            } else {
                result.append((range: offset..<next, kind: classify(scalar) ?? result.last?.kind ?? .other))
            }
            offset = next
        }
        return result
    }

    private static func joins(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.value {
        case 0x3099, 0x309A, 0x200D, 0xFE00...0xFE0F, 0xE0100...0xE01EF: return true
        default: return CharacterSet.nonBaseCharacters.contains(scalar)
        }
    }

    /// nil: the kind of the character before it (prolonged sound and sound marks).
    private static func classify(_ scalar: Unicode.Scalar) -> Kind? {
        switch scalar.value {
        case 0x309B, 0x309C, 0x30FC, 0xFF70, 0xFF9E, 0xFF9F:
            return nil
        case 0x3005...0x3007, 0x303B, 0x3400...0x4DBF, 0x4E00...0x9FFF, 0xF900...0xFAFF, 0x20000...0x3134F:
            return .kanji
        case 0x3041...0x3096, 0x309D...0x309F:
            return .hiragana
        case 0x30A1...0x30FA, 0x30FD...0x30FF, 0x31F0...0x31FF, 0xFF66...0xFF6F, 0xFF71...0xFF9D:
            return .katakana
        default:
            break
        }
        if CharacterSet.whitespacesAndNewlines.contains(scalar) { return .space }
        if CharacterSet.alphanumerics.contains(scalar) { return .letters }
        return .other
    }
}

/// Taps in a row (double and triple tap) counted without waiting: a touch that starts within
/// `window` seconds after the last tap ended, within `slop` points of it, continues the row.
/// Only a single tap's own action (the word lookup) waits, `singleTapDelay`; a second tap in
/// that time drops it (a touch that turns into a scroll does not).
public struct TapSequence {
    /// How long a single tap's action waits for a second tap (at most 0.25 s).
    public static let singleTapDelay = 0.25
    public var window: Double
    public var slop: Double
    private var count = 0
    private var continuing = false
    private var lastTime = -Double.infinity
    private var lastX = 0.0
    private var lastY = 0.0

    public init(window: Double = 0.3, slop: Double = 30) { self.window = window; self.slop = slop }

    private func near(_ x: Double, _ y: Double) -> Bool {
        (x - lastX) * (x - lastX) + (y - lastY) * (y - lastY) <= slop * slop
    }

    /// Whether a touch starting now (seconds, points) would continue the row. Such a touch is
    /// a tap, not a grab of a selection handle (after two taps the handles cover a short word).
    public func wouldContinue(time: Double, x: Double, y: Double) -> Bool {
        count > 0 && time - lastTime <= window && near(x, y)
    }

    /// A finger touched down: true when it continues the row. Only touches near the last tap
    /// count; a finger elsewhere does not break the row.
    @discardableResult
    public mutating func touchBegan(time: Double, x: Double, y: Double) -> Bool {
        guard near(x, y) else { return false }
        continuing = wouldContinue(time: time, x: x, y: y)
        return continuing
    }

    /// A tap ended: 1 single, 2 double, 3 triple (a fourth tap starts a new row).
    public mutating func tapEnded(time: Double, x: Double, y: Double) -> Int {
        count = continuing && near(x, y) && count < 3 ? count + 1 : 1
        continuing = false
        lastTime = time; lastX = x; lastY = y
        return count
    }

    /// Forgets the row (the single tap's action ran).
    public mutating func reset() { count = 0; continuing = false; lastTime = -Double.infinity }
}

/// Scrolling the reader while a selection end is dragged near its top or bottom edge.
public enum TextSelectionAutoscroll {
    /// Points to scroll per 60 Hz frame (negative: up) for a finger at `y` with the visible text
    /// from `top` to `bottom`: inside `zone` points of an edge, faster closer to it and past it.
    public static func step(y: Double, top: Double, bottom: Double, zone: Double = 54, fastest: Double = 16) -> Double {
        guard zone > 0, bottom - top > zone * 2 else { return 0 }
        if y < top + zone { return -fastest * max(0.15, min(1, (top + zone - y) / zone)) }
        if y > bottom - zone { return fastest * max(0.15, min(1, (y - (bottom - zone)) / zone)) }
        return 0
    }
}

public enum TextSelectionGeometry {
    /// The paragraph whose span (top...bottom, on screen) holds `y`, otherwise the nearest one;
    /// the earlier paragraph when two are as near.
    public static func nearest(_ spans: [(paragraph: Int, top: Double, bottom: Double)], y: Double) -> Int? {
        var best: (paragraph: Int, distance: Double)?
        for span in spans {
            let distance = y < span.top ? span.top - y : (y > span.bottom ? y - span.bottom : 0)
            if let current = best, current.distance < distance || (current.distance == distance && current.paragraph < span.paragraph) {
                continue
            }
            best = (span.paragraph, distance)
        }
        return best?.paragraph
    }

    /// A paragraph view's frame on screen (window coordinates), for `nearest(_:x:y:)`.
    public struct Span: Equatable, Sendable {
        public let paragraph: Int
        public let minX: Double, minY: Double, maxX: Double, maxY: Double
        public init(paragraph: Int, minX: Double, minY: Double, maxX: Double, maxY: Double) {
            self.paragraph = paragraph; self.minX = minX; self.minY = minY; self.maxX = maxX; self.maxY = maxY
        }
    }

    /// The paragraph under a point, otherwise the nearest one in both directions (a point in the
    /// margins beside a centred column, as on an iPad in landscape, goes to the paragraph beside
    /// it; side-by-side columns stay apart); the earlier paragraph when two are as near.
    public static func nearest(_ spans: [Span], x: Double, y: Double) -> Int? {
        pick(spans, x: x, y: y).map { spans[$0].paragraph }
    }

    /// Which paragraph view a point goes to, as its place in `spans` (the caller maps the touch
    /// in exactly that view: two views can show one paragraph, as when SwiftUI keeps the copy
    /// laid out before a rotation next to the one on screen, and looking the view up again by
    /// its paragraph could take the other copy, whose lines are somewhere else on screen).
    /// `under`: the view UIKit's hit-testing found under the finger (the one whose drawing is
    /// there), taken when its frame holds the point. Otherwise the span under the point or the
    /// nearest one; the earlier paragraph when two are as near, then the earlier span.
    public static func pick(_ spans: [Span], x: Double, y: Double, under: Int? = nil) -> Int? {
        func valid(_ span: Span) -> Bool { span.maxX >= span.minX && span.maxY >= span.minY }
        if let under, spans.indices.contains(under) {
            let span = spans[under]
            if valid(span), x >= span.minX, x <= span.maxX, y >= span.minY, y <= span.maxY { return under }
        }
        var best: (index: Int, distance: Double)?
        for (index, span) in spans.enumerated() where valid(span) {
            let dx = x < span.minX ? span.minX - x : (x > span.maxX ? x - span.maxX : 0)
            let dy = y < span.minY ? span.minY - y : (y > span.maxY ? y - span.maxY : 0)
            let distance = dx * dx + dy * dy
            if let current = best,
               current.distance < distance || (current.distance == distance && spans[current.index].paragraph <= span.paragraph) {
                continue
            }
            best = (index, distance)
        }
        return best?.index
    }

    /// A selection handle's touch target around its drawn frame: at least 44 × 44 points, centred
    /// on it (all in the same coordinates).
    public static func handleTarget(minX: Double, minY: Double, width: Double, height: Double,
                                    least: Double = 44) -> (minX: Double, minY: Double, width: Double, height: Double) {
        let grow = (x: max(0, least - width) / 2, y: max(0, least - height) / 2)
        return (minX - grow.x, minY - grow.y, width + grow.x * 2, height + grow.y * 2)
    }
}

/// Lines of a paragraph laid out by CoreText, and the line a touch is on. CoreText's y runs up
/// from the text's bottom edge, which is `flipY` points below the view's top (the view's
/// height less the handle room): the same numbers place the lines when they are drawn and when a
/// touch is mapped to them, so both need the geometry of one layout (JapaneseTextView keeps
/// the one it last drew).
public enum TextLineGeometry {
    public struct Line: Equatable, Sendable {
        /// The baseline's height above the text's bottom edge, and the line's ascent and descent.
        public let originY: Double, ascent: Double, descent: Double
        public init(originY: Double, ascent: Double, descent: Double) {
            self.originY = originY; self.ascent = ascent; self.descent = descent
        }
        /// The line's top and bottom in view coordinates (y down from the view's top).
        public func top(flipY: Double) -> Double { flipY - (originY + ascent) }
        public func bottom(flipY: Double) -> Double { flipY - (originY - descent) }
    }

    /// The line nearest to a point's height `y` in view coordinates (by the middle of its
    /// glyphs); the earlier line when two are as near. nil without lines.
    public static func nearest(_ lines: [Line], flipY: Double, y: Double) -> Int? {
        let target = flipY - y
        var best: (index: Int, distance: Double)?
        for (index, line) in lines.enumerated() {
            let distance = abs(line.originY + (line.ascent - line.descent) / 2 - target)
            if let current = best, current.distance <= distance { continue }
            best = (index, distance)
        }
        return best?.index
    }
}
