import Foundation

/// A word in Japanese text: UTF-16 range, Jiten identity and knowledge state.
/// The Mac sends `[start, end, wordId, readingIndex, state]`.
public struct StudyToken: Codable, Hashable {
    public let start: Int
    public let end: Int
    public let wordId: Int
    public let readingIndex: Int
    /// 0 new/unknown, 1 learning (young), 2 due, 3 known (mature/mastered/redundant), 4 ignored (blacklisted/suspended).
    public let state: Int
    /// Bit 1: the Mac reader shows furigana for it; bit 2: the Mac reader marks it.
    public let flags: Int
    public init(start: Int, end: Int, wordId: Int, readingIndex: Int, state: Int, flags: Int = 3) {
        self.start = start; self.end = end; self.wordId = wordId; self.readingIndex = readingIndex; self.state = state; self.flags = flags
    }
    public var wantsFurigana: Bool { flags & 1 != 0 }
    public var wantsMark: Bool { flags & 2 != 0 }
    /// Mature/mastered on Jiten.
    public var isMature: Bool { flags & 4 != 0 || state == 3 }
    public var key: String { "\(wordId):\(readingIndex)" }
    /// Due for review on Jiten (the Mac's state code 2 is its "due" word state).
    public var isDue: Bool { state == 2 || wordState == "due" }
    /// The Mac reader's word state (flags bits 7-10, one of `ReaderLook.wordStates`); nil from older Macs.
    public var wordState: String? {
        let index = (flags >> 7) & 15
        return index > 0 && index <= ReaderLook.wordStates.count ? ReaderLook.wordStates[index - 1] : nil
    }
    /// JitenMPV KnownState (0 New, 1 Young, 2 Mature, 3 Blacklisted, 4 Due, 5 Mastered, 6 Redundant, 7 Suspended).
    /// A due word is Due whatever else it is, as on the Mac (older Macs sent e.g. Mastered for a
    /// mastered-and-due word: its subtitle colour said Mastered while the reader said Due).
    public var jitenState: Int {
        if isDue { return 4 }
        let encoded = (flags >> 3) & 15
        if encoded > 0 { return encoded - 1 }
        switch state { case 1: return 1; case 2: return 4; case 3: return flags & 4 != 0 ? 5 : 2; case 4: return 3; default: return 0 }
    }
    public func with(state: Int, flags: Int) -> StudyToken {
        StudyToken(start: start, end: end, wordId: wordId, readingIndex: readingIndex, state: state, flags: flags)
    }
    public init(from decoder: Decoder) throws {
        var row = try decoder.unkeyedContainer()
        start = try row.decode(Int.self); end = try row.decode(Int.self)
        wordId = try row.decode(Int.self); readingIndex = try row.decode(Int.self)
        state = try row.isAtEnd ? 0 : row.decode(Int.self)
        flags = try row.isAtEnd ? 3 : row.decode(Int.self)
    }
    public func encode(to encoder: Encoder) throws {
        var row = encoder.unkeyedContainer()
        try row.encode(start); try row.encode(end); try row.encode(wordId); try row.encode(readingIndex); try row.encode(state); try row.encode(flags)
    }
    public var hasEntry: Bool { wordId > 0 }
}

/// Text split into plain gaps and tappable words (UTF-16 offsets, never overlapping).
public struct StudySegment: Hashable {
    public let text: String
    /// Index into the token array, nil for text between words.
    public let token: Int?
}

public enum StudyText {
    public static func segments(_ text: String, tokens: [StudyToken]) -> [StudySegment] {
        let units = Array(text.utf16)
        func piece(_ low: Int, _ high: Int) -> String {
            String(decoding: units[max(0, low)..<min(units.count, high)], as: UTF16.self)
        }
        var result: [StudySegment] = []
        var cursor = 0
        for (index, token) in tokens.enumerated() {
            guard token.start >= cursor, token.start < units.count, token.end > token.start else { continue }
            let end = min(units.count, token.end)
            if token.start > cursor { result.append(StudySegment(text: piece(cursor, token.start), token: nil)) }
            result.append(StudySegment(text: piece(token.start, end), token: index))
            cursor = end
        }
        if cursor < units.count { result.append(StudySegment(text: piece(cursor, units.count), token: nil)) }
        return result
    }

    /// The sentence around a word, sent with Add/Review like the Mac reader does.
    public static func sentence(_ text: String, around token: StudyToken) -> String {
        let units = Array(text.utf16)
        let stops = Set("。！？!?…\n".utf16)
        var low = min(max(0, token.start), units.count)
        var high = min(max(low, token.end), units.count)
        while low > 0, !stops.contains(units[low - 1]) { low -= 1 }
        while high < units.count, !stops.contains(units[high]) { high += 1 }
        if high < units.count { high += 1 }
        return String(decoding: units[low..<high], as: UTF16.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// Furigana over a UTF-16 range of a paragraph; the Mac sends `[start, end, reading]`.
public struct Furigana: Codable, Hashable {
    public let start: Int
    public let end: Int
    public let reading: String
    public init(start: Int, end: Int, reading: String) { self.start = start; self.end = end; self.reading = reading }
    public init(from decoder: Decoder) throws {
        var row = try decoder.unkeyedContainer()
        start = try row.decode(Int.self); end = try row.decode(Int.self); reading = try row.decode(String.self)
    }
    public func encode(to encoder: Encoder) throws {
        var row = encoder.unkeyedContainer()
        try row.encode(start); try row.encode(end); try row.encode(reading)
    }
}

/// Live Jiten state of a word, `[wordId, readingIndex, state, flags]` from the Mac.
public struct StudyStateRow: Codable, Hashable {
    public let wordId: Int
    public let readingIndex: Int
    public let state: Int
    public let flags: Int
    public var key: String { "\(wordId):\(readingIndex)" }
    public init(wordId: Int, readingIndex: Int, state: Int, flags: Int) {
        self.wordId = wordId; self.readingIndex = readingIndex; self.state = state; self.flags = flags
    }
    public init(from decoder: Decoder) throws {
        var row = try decoder.unkeyedContainer()
        wordId = try row.decode(Int.self); readingIndex = try row.decode(Int.self)
        state = try row.decode(Int.self); flags = try row.decode(Int.self)
    }
    public func encode(to encoder: Encoder) throws {
        var row = encoder.unkeyedContainer()
        try row.encode(wordId); try row.encode(readingIndex); try row.encode(state); try row.encode(flags)
    }
}

public struct StudyStates: Codable { public let rows: [StudyStateRow] }

extension Array where Element == StudyToken {
    /// Tokens with fresher states (words not in `states` keep theirs).
    public func updating(_ states: [String: StudyStateRow]) -> [StudyToken] {
        map { token in states[token.key].map { token.with(state: $0.state, flags: $0.flags) } ?? token }
    }
}

public struct TextParagraph: Codable, Hashable {
    public let text: String
    public let image: Bool
    public var tokens: [StudyToken]
    public let ruby: [Furigana]?
    /// An illustration's file on the Mac (`media/ln-image/<imageId>`); nil for text.
    public let imageId: String?
    public init(text: String, image: Bool = false, tokens: [StudyToken], ruby: [Furigana]? = nil, imageId: String? = nil) {
        self.text = text; self.image = image; self.tokens = tokens; self.ruby = ruby; self.imageId = imageId
    }
    /// Index of the word that covers a UTF-16 offset.
    public func token(at offset: Int) -> Int? {
        tokens.firstIndex { $0.start <= offset && offset < $0.end }
    }
}

/// How the Mac's reader draws words (Settings → Light novels → Reader).
public struct ReaderLook: Codable, Hashable {
    public let markStyle: String
    public let showFurigana: Bool
    public let font: String
    public let lineHeight: Double
    public let textColor: String
    public let backgroundColor: String
    public let colors: [String: String]
    /// The Mac reader blurs illustrations until they are tapped.
    public let blurImages: Bool?
    /// false: "Auto-bookmark after 20 s" is off on the Mac, so no automatic read markers.
    public let autoBookmarks: Bool?
    public init(markStyle: String = "underline", showFurigana: Bool = true, font: String = "mincho", lineHeight: Double = 1.9,
                textColor: String = "#c9c7c2", backgroundColor: String = "#000000",
                colors: [String: String] = ReaderLook.defaultColors,
                blurImages: Bool? = nil, autoBookmarks: Bool? = nil) {
        self.markStyle = markStyle; self.showFurigana = showFurigana; self.font = font; self.lineHeight = lineHeight
        self.textColor = textColor; self.backgroundColor = backgroundColor; self.colors = colors; self.blurImages = blurImages
        self.autoBookmarks = autoBookmarks
    }
    /// The Mac's nine word states (Jiten's, "not_in_deck" = New and in no study deck).
    public static let wordStates = ["new", "not_in_deck", "young", "mature", "mastered", "due", "blacklisted", "suspended", "redundant"]
    /// The Mac's default colours (every state has one). "learning"/"known" are the older codes 1 and 3.
    public static let defaultColors: [String: String] = [
        "new": "#f43f5e", "not_in_deck": "#f43f5e", "young": "#f59e0b", "mature": "#4ade80", "mastered": "#2dd4bf", "due": "#f97316",
        "blacklisted": "#9ca3af", "suspended": "#9ca3af", "redundant": "#0ea5e9", "learning": "#f59e0b", "known": "#2dd4bf",
    ]
    /// Colour (hex) for a knowledge state code; an empty or missing colour (older Macs) is the default.
    public func color(state: Int) -> String {
        let key = ["new", "learning", "due", "known", "blacklisted"][min(max(0, state), 4)]
        return ReaderLook.hex(colors[key], key)
    }
    /// Colour (hex) of a word by the Mac's word state.
    public func color(for token: StudyToken) -> String? {
        guard let state = token.wordState else { return color(state: token.state) }
        return ReaderLook.hex(colors[state], state)
    }
    static func hex(_ value: String?, _ key: String) -> String {
        if let value = value, !value.trimmingCharacters(in: .whitespaces).isEmpty { return value }
        return ReaderLook.defaultColors[key] ?? "#9aa8ba"
    }
}

public struct ParsedText: Codable {
    public var paragraphs: [TextParagraph]
    public init(paragraphs: [TextParagraph]) { self.paragraphs = paragraphs }
}

public struct ItemsResponse<Item: Codable>: Codable { public let items: [Item] }

// MARK: Light novels

public struct LightNovelItem: Codable, Hashable, Identifiable {
    public let id: Int
    public let title: String
    public let volume: Int?
    public let chapters: Int
    public let chapter: Int
    public let offset: Double
    public let finished: Bool
    public let hasCover: Bool
    public let hasAudio: Bool
    public let updatedAt: Double
    // Grouping as on the Mac (optional for older Macs).
    public let seriesKey: String?
    public let seriesTitle: String?
    public let franchiseKey: String?
    public let franchiseTitle: String?
    public let franchiseOrder: Int?
    public let progress: Double?
    /// Characters of the volume and its AniList year (the Mac card's chips; absent from older Macs).
    public let characters: Int?
    public let year: Int?
    /// The phone assets of its linked audiobooks (absent from older Macs): a finished
    /// audiobook offers to mark this volume read.
    public var audioAssetIds: [String]? = nil
}

public struct LightNovelChapterInfo: Codable, Hashable, Identifiable {
    public let index: Int
    public let title: String
    public let characters: Int
    public var id: Int { index }
}

public struct LightNovelDetail: Codable {
    public let id: Int
    public let title: String
    /// Linked audiobook on the iPhone protocol (Read and listen), if any.
    public let audioAssetId: String?
    public let chapter: Int
    public let offset: Double
    public let finished: Bool
    public let chapters: [LightNovelChapterInfo]
    /// The volume's "read up to here" marker (older Macs send none).
    public var marker: ReadMarker? = nil
}

/// The "read up to here" marker of a volume, one for the Mac and every iPhone/iPad
/// (pudge/ln_read_marker.py): `paragraph` is the reader paragraph, `paragraphOffset` the
/// UTF-16 offset in its text, `kind` "manual" (Read up to here) or "auto" (reading on, the
/// audio), `at` when (seconds), `here` set on this device.
public struct ReadMarker: Codable, Hashable {
    public let chapter: Int
    public let paragraph: Int
    public let paragraphOffset: Int
    public let char: Int?
    public let kind: String
    public let at: Double
    public let device: String?
    public let deviceName: String?
    public let here: Bool?
    public init(chapter: Int, paragraph: Int, paragraphOffset: Int, char: Int? = nil, kind: String, at: Double,
                device: String? = nil, deviceName: String? = nil, here: Bool? = nil) {
        self.chapter = chapter; self.paragraph = paragraph; self.paragraphOffset = paragraphOffset; self.char = char
        self.kind = kind; self.at = at; self.device = device; self.deviceName = deviceName; self.here = here
    }
}

public struct ChapterText: Codable {
    public let parsing: Bool
    public let index: Int
    public let title: String
    public var paragraphs: [TextParagraph]
    /// Reader paragraph of a read-along paragraph number (illustrations skipped).
    public func readerParagraph(textIndex: Int) -> Int? {
        var count = 0
        for (index, paragraph) in paragraphs.enumerated() where !paragraph.image {
            if count == textIndex { return index }
            count += 1
        }
        return nil
    }
    /// Paragraph that holds a saved chapter fraction (0...1), counted by characters.
    public func paragraph(atFraction fraction: Double) -> Int {
        let sizes = paragraphs.map { max(1, $0.text.count) }
        let total = sizes.reduce(0, +)
        guard total > 0 else { return 0 }
        var remaining = Int(Double(total) * min(1, max(0, fraction)))
        for (index, size) in sizes.enumerated() {
            if remaining < size { return index }
            remaining -= size
        }
        return max(0, paragraphs.count - 1)
    }
    /// Paragraph index as the read-along text counts it (illustrations skipped).
    public func textParagraph(_ index: Int) -> Int {
        paragraphs.prefix(max(0, index)).filter { !$0.image }.count
    }
    public func fraction(atParagraph index: Int) -> Double {
        let sizes = paragraphs.map { max(1, $0.text.count) }
        let total = sizes.reduce(0, +)
        guard total > 0 else { return 0 }
        return Double(sizes.prefix(max(0, index)).reduce(0, +)) / Double(total)
    }
}

// MARK: Manga

public struct MangaItem: Codable, Hashable, Identifiable {
    public let id: Int
    public let title: String
    public let pages: Int
    public let position: Int
    public let readPages: Int
    public let rightToLeft: Bool
    public let ocrPages: Int
    public let updatedAt: Double
    public let seriesKey: String?
    public let seriesTitle: String?
    public let volume: Int?
}

/// OCR text box, normalized 0...1 with a top-left origin.
public struct MangaRegion: Codable, Hashable {
    public let x: Double
    public let y: Double
    public let width: Double
    public let height: Double
    public let text: String
    public let vertical: Bool
}

public struct MangaPageText: Codable {
    public let page: Int
    public let status: String
    public let regions: [MangaRegion]
}

/// `media/manga/<id>/spreads`: the pages that are one wide picture (a double-page spread, shown
/// alone in the two-page view), the usual page's height / width (nil: not known), the
/// volume's "Shift pairs by one page" and "Align scanned pages" and the pages' transforms (nil:
/// an older Mac, or one that cannot work them out). The Mac also sends its `spreads` table;
/// the phone makes the same one (MangaSpreads.spreads).
public struct MangaSpreadPages: Codable, Equatable, Sendable {
    public let wide: [Int]
    public let aspect: Double?
    public let shift: Bool?
    public let align: Bool?
    public let alignment: MangaAlignment?
    public init(wide: [Int], aspect: Double?, shift: Bool? = nil, align: Bool? = nil, alignment: MangaAlignment? = nil) {
        self.wide = wide; self.aspect = aspect; self.shift = shift; self.align = align; self.alignment = alignment
    }
}

/// `POST media/manga/<id>/spreads`: "Shift pairs by one page" on or off for the volume.
public struct MangaPairShiftRequest: Codable { public let shift: Bool; public init(shift: Bool) { self.shift = shift } }

/// `POST media/manga/<id>/spreads`: "Align scanned pages" on or off for the volume.
public struct MangaPageAlignRequest: Codable { public let align: Bool; public init(align: Bool) { self.align = align } }

// MARK: Video

public struct EpisodeItem: Codable, Hashable, Identifiable {
    public let id: Int
    public let mediaId: Int
    public let number: Int
    public let title: String
    public let episodeTitle: String
    public let watched: Bool
    public let position: Double
    public let duration: Double
    public let prepared: Bool
    public let preparing: Bool
    /// Changes when the Mac's subtitle changes.
    public let subtitleRevision: String?
    /// The Mac shows raw subtitles it could not align (timing may be off). Absent from older Macs.
    public let subtitleNotAligned: Bool?
    /// The copies the phone may download or stream, with their sizes (absent from older Macs).
    public let qualities: [VideoQualityOption]?
    /// The Mac's mpv delay for this subtitle (ms, + later); absent from older Macs.
    public let subtitleDelayMs: Int?
    /// Detected opening/ending of the file (the Skip button); absent from older Macs.
    public let skipSegments: [SkipSegment]?
    /// An episode kept on the device that the Mac no longer lists (the Watch list builds these
    /// on every redraw: no JSON round trip, nothing that can trap).
    public init(id: Int, mediaId: Int, number: Int, title: String, episodeTitle: String, watched: Bool,
                position: Double, duration: Double, prepared: Bool, preparing: Bool,
                subtitleRevision: String? = nil, subtitleNotAligned: Bool? = nil, qualities: [VideoQualityOption]? = nil,
                subtitleDelayMs: Int? = nil, skipSegments: [SkipSegment]? = nil) {
        self.id = id; self.mediaId = mediaId; self.number = number; self.title = title; self.episodeTitle = episodeTitle
        self.watched = watched; self.position = position; self.duration = duration; self.prepared = prepared; self.preparing = preparing
        self.subtitleRevision = subtitleRevision; self.subtitleNotAligned = subtitleNotAligned; self.qualities = qualities
        self.subtitleDelayMs = subtitleDelayMs; self.skipSegments = skipSegments
    }
}

/// A copy of an episode: the original or a smaller re-encode the Mac makes (to keep more
/// episodes on the device, like a video site's download quality).
public enum VideoQuality: String, CaseIterable, Codable, Sendable {
    case original, smaller, saver

    public static let downloadKey = "pudge.downloadQuality"
    public static let streamKey = "pudge.streamQuality"

    /// Only the resolution (the pickers and the Download menu): "720p", "540p". The original keeps
    /// the file's own resolution, which differs between releases, so it stays "Original".
    public var title: String {
        switch self {
        case .original: return "Original"
        case .smaller: return "720p"
        case .saver: return "540p"
        }
    }

    /// Bits per second of a re-encode (video + audio, as the Mac makes it); nil: the original.
    public var bitsPerSecond: Double? {
        switch self {
        case .original: return nil
        case .smaller: return (1400 + 96) * 1000
        case .saver: return (750 + 64) * 1000
        }
    }

    /// Bytes of a re-encode of `seconds` (nil: the original, or no duration).
    public func estimatedBytes(seconds: Double) -> Int64? {
        guard let bitsPerSecond, seconds.isFinite, seconds > 0 else { return nil }
        return Int64(bitsPerSecond / 8 * seconds * 1.015)
    }

    /// The choice kept in Settings (`key`); the original when none was made.
    public static func stored(_ key: String, _ defaults: UserDefaults = .standard) -> VideoQuality {
        defaults.string(forKey: key).flatMap(VideoQuality.init(rawValue:)) ?? .original
    }

    /// The size the phone shows for this copy: the Mac's (exact once prepared), else estimated
    /// from the episode's duration; nil: not known (an original not sized by the Mac yet).
    public func size(in options: [VideoQualityOption]?, durationSeconds: Double) -> (bytes: Int64, estimated: Bool)? {
        if let option = options?.first(where: { $0.id == rawValue }), let bytes = option.sizeBytes, bytes > 0 {
            return (bytes: bytes, estimated: option.estimated ?? false)
        }
        return estimatedBytes(seconds: durationSeconds).map { (bytes: $0, estimated: true) }
    }
}

/// The Mac's word on one copy of an episode.
public struct VideoQualityOption: Codable, Hashable, Sendable {
    public let id: String
    public let sizeBytes: Int64?
    public let prepared: Bool?
    public let estimated: Bool?
    public init(id: String, sizeBytes: Int64?, prepared: Bool?, estimated: Bool?) {
        self.id = id; self.sizeBytes = sizeBytes; self.prepared = prepared; self.estimated = estimated
    }
}

/// `POST media/episodes/<id>/prepare|stream`: the copy wanted (nil: the original, `{}` as before).
public struct VideoQualityRequest: Codable, Sendable {
    public let quality: String?
    public init(_ quality: VideoQuality) { self.quality = quality == .original ? nil : quality.rawValue }
}

public struct ResourceRef: Codable, Hashable {
    public let resourceId: String
    public let sha256: String
    public let sizeBytes: Int64
    public let fileName: String
}

public struct EpisodeState: Codable {
    public let id: Int
    public let title: String
    public let number: Int
    public let state: String
    public let progress: Double
    public let message: String
    public let video: ResourceRef?
    public let durationMs: Int64?
    /// The copy this state is about and every copy's size (absent from older Macs).
    public let quality: String?
    public let qualities: [VideoQualityOption]?
}

public struct SubtitleCue: Codable, Hashable {
    public let start: Int64
    public let end: Int64
    public let text: String
    public var tokens: [StudyToken]
    public let ruby: [Furigana]?
}

public struct SubtitleTrack: Codable {
    public let schema: Int
    public let tokenized: Bool
    public var cues: [SubtitleCue]
    public init(schema: Int = 1, tokenized: Bool, cues: [SubtitleCue]) { self.schema = schema; self.tokenized = tokenized; self.cues = cues }

    /// Cue shown at a playback time (cues sorted by start; the latest started cue that has not ended).
    public func cue(atMs time: Int64) -> Int? {
        var low = 0, high = cues.count - 1, found = -1
        while low <= high {
            let middle = (low + high) / 2
            if cues[middle].start <= time { found = middle; low = middle + 1 } else { high = middle - 1 }
        }
        var index = found
        while index >= 0, index > found - 4 {
            if cues[index].end > time { return index }
            index -= 1
        }
        return nil
    }
}

// MARK: Study

public struct WordDetail: Codable, Identifiable {
    public let wordId: Int
    public let readingIndex: Int
    public let spelling: String
    public let reading: String
    public let readings: [String]
    public let meanings: [String]
    public let state: Int
    public let stateLabel: String
    public let frequencyRank: Int?
    public let inDeck: Bool?
    public let complete: Bool
    public var id: String { "\(wordId):\(readingIndex)" }
}

public struct StudyDeck: Codable, Hashable, Identifiable { public let id: String; public let name: String }
public struct StudyGrade: Codable, Hashable, Identifiable { public let id: String; public let label: String; public let tone: String }

public struct StudyOptions: Codable {
    public let backend: String
    public let decks: [StudyDeck]
    public let deckId: String
    public let grades: [StudyGrade]
    public let message: String
}

public struct StudyActionResult: Codable {
    public let ok: Bool
    public let outcome: String
    public let message: String
    /// The word's state after the grade or add, `[wordId, readingIndex, state, flags]`; nil from
    /// older Macs or when Jiten's answer was not fresh (the phone then asks for it).
    public let row: StudyStateRow?
    public init(ok: Bool, outcome: String, message: String, row: StudyStateRow? = nil) {
        self.ok = ok; self.outcome = outcome; self.message = message; self.row = row
    }
}

/// The Mac's personal weekly schedule for a title.
public struct AnimeSchedule: Codable, Hashable {
    public let status: String
    public let nextEpisode: Int?
    public let nextUnlockAt: Double?
}

/// One anime with the episodes the Mac offers now (Anime page rules).
public struct AnimeGroup: Codable, Hashable, Identifiable {
    public let mediaId: Int
    public let title: String
    public let progress: Int
    public let total: Int?
    public let schedule: AnimeSchedule?
    /// Ongoing on the Mac (airing, a personal schedule, or its New episodes ready); nil from an older Mac.
    public let airing: Bool?
    /// A movie on AniList (the Mac's "Movie • resume at …"); nil from an older Mac.
    public let movie: Bool?
    public var episodes: [EpisodeItem]
    public var id: String { mediaId > 0 ? "anime:\(mediaId)" : "title:\(title):\(episodes.first?.id ?? 0)" }
    public init(mediaId: Int, title: String, progress: Int = 0, total: Int? = nil, schedule: AnimeSchedule? = nil, airing: Bool? = nil,
                movie: Bool? = nil, episodes: [EpisodeItem]) {
        self.mediaId = mediaId; self.title = title; self.progress = progress; self.total = total; self.schedule = schedule
        self.airing = airing; self.movie = movie; self.episodes = episodes
    }
}

/// A Mac Home card of a title with nothing to play yet (GET media/anime `home`; nil from an older Mac).
/// `section`: the Mac's caught_up, needs_action, waiting or download_available; `status`: its episode
/// presentation (downloading, waiting_subtitles, missing, …); `nextAt`: next airing or unlock (seconds).
/// `actionCode` / `extraKinds`: an Action required card's reason; `downloadKind`: its torrent's state as
/// the Mac card names it (downloading, paused, waiting, stalled, checking, error, finished, unknown);
/// `queuedRelease`: a release waits for torrents to be turned on. Absent from older Macs.
public struct MacHomeCard: Codable, Hashable, Identifiable {
    public let mediaId: Int
    public let title: String
    public let progress: Int
    public let total: Int?
    public let section: String
    public let status: String?
    public let nextEpisode: Int?
    public let nextAt: Double?
    public let downloadFraction: Double?
    public let downloadKind: String?
    public let queuedRelease: Bool?
    public let actionCode: String?
    public let extraKinds: [String]?
    public var id: Int { mediaId }
    public init(mediaId: Int, title: String, progress: Int = 0, total: Int? = nil, section: String, status: String? = nil,
                nextEpisode: Int? = nil, nextAt: Double? = nil, downloadFraction: Double? = nil, downloadKind: String? = nil,
                queuedRelease: Bool? = nil, actionCode: String? = nil, extraKinds: [String]? = nil) {
        self.mediaId = mediaId; self.title = title; self.progress = progress; self.total = total; self.section = section
        self.status = status; self.nextEpisode = nextEpisode; self.nextAt = nextAt; self.downloadFraction = downloadFraction
        self.downloadKind = downloadKind; self.queuedRelease = queuedRelease; self.actionCode = actionCode; self.extraKinds = extraKinds
    }
}

/// The Mac Home's Caught up and its Waiting / Available to download titles, in the Mac's order.
public struct MacHomeShelves: Codable, Hashable {
    public let caughtUp: [MacHomeCard]
    public let waiting: [MacHomeCard]
    public init(caughtUp: [MacHomeCard], waiting: [MacHomeCard]) { self.caughtUp = caughtUp; self.waiting = waiting }
}

// MARK: JitenMPV look for subtitles

/// One word style from the user's JitenMPV theme (mpv units: sizes relative to `fontSize`).
public struct JitenWordStyle: Codable, Hashable {
    public let text: String
    public let outline: String
    public let outlineSize: Double
    public let opacity: Int?
    public let bold: Bool?
    public let italic: Bool?
    public let underline: Bool?
    public let underlineColor: String?
    public let underlineThickness: Double?
    public let shadow: String?
}

public struct SubtitleLook: Codable, Hashable {
    public let source: String
    public let theme: String
    public let fontFamily: String
    public let fontSize: Double
    public let borderSize: Double
    public let unparsed: JitenWordStyle
    /// Indexed by `StudyToken.jitenState`.
    public let states: [JitenWordStyle]
    public func style(_ token: StudyToken?) -> JitenWordStyle {
        guard let token, token.hasEntry, states.indices.contains(token.jitenState) else { return unparsed }
        return states[token.jitenState]
    }
}

// MARK: Library shelves (franchise → series → volumes, as on the Mac)

public struct ShelfSeries<Item: Identifiable>: Identifiable {
    public let key: String
    public let title: String
    public let items: [Item]
    public var id: String { key }
}

public struct Shelf<Item: Identifiable>: Identifiable {
    public let key: String
    public let title: String
    public let series: [ShelfSeries<Item>]
    public var id: String { key }
    public var items: [Item] { series.flatMap(\.items) }
}

public enum Shelves {
    /// Groups by franchise (when known), then series; volumes sorted, shelves by most recent activity.
    public static func group<Item: Identifiable>(_ items: [Item], franchise: (Item) -> (key: String, title: String, order: Int)?,
                                                 series: (Item) -> (key: String, title: String),
                                                 volume: (Item) -> Int, updated: (Item) -> Double) -> [Shelf<Item>] {
        var shelves: [String: (title: String, series: [String: (title: String, order: Int, items: [Item])])] = [:]
        var order: [String] = []
        for item in items {
            let seriesInfo = series(item)
            let franchiseInfo = franchise(item)
            let shelfKey = franchiseInfo.map { "franchise:" + $0.key } ?? "series:" + seriesInfo.key
            if shelves[shelfKey] == nil { shelves[shelfKey] = (franchiseInfo?.title ?? seriesInfo.title, [:]); order.append(shelfKey) }
            var entry = shelves[shelfKey]!.series[seriesInfo.key] ?? (seriesInfo.title, franchiseInfo?.order ?? 0, [])
            entry.items.append(item)
            shelves[shelfKey]!.series[seriesInfo.key] = entry
        }
        let result: [Shelf<Item>] = order.map { key in
            let value = shelves[key]!
            let series = value.series.map { seriesKey, entry in
                ShelfSeries(key: seriesKey, title: entry.title, items: entry.items.sorted { volume($0) < volume($1) })
            }.sorted { left, right in
                let a = value.series[left.key]!.order, b = value.series[right.key]!.order
                return a != b ? a < b : left.title.localizedStandardCompare(right.title) == .orderedAscending
            }
            return Shelf(key: key, title: value.title, series: series)
        }
        return result.sorted { left, right in
            (left.items.map(updated).max() ?? 0) > (right.items.map(updated).max() ?? 0)
        }
    }
}

// MARK: Review gate before an episode

public struct GateCard: Codable, Hashable, Identifiable {
    public let wordId: Int
    public let readingIndex: Int
    public let word: String
    public let reading: String
    public let meanings: [String]
    public let sentence: String
    public let frequencyRank: Int?
    public var id: String { "\(wordId):\(readingIndex)" }
}

public struct GateStatus: Codable {
    public let enabled: Bool
    public let blocking: Bool
    public let granted: Bool
    public let required: Int
    public let completed: Int
    public let remaining: Int
    public let reason: String
    public let outcome: String
    public let message: String
    public let grades: [StudyGrade]
    public let cards: [GateCard]
}

// MARK: Statistics and planning

public struct StatsDay: Codable, Hashable, Identifiable {
    public let day: String
    public let seconds: Double
    public let breakdown: [String: Double]
    public var id: String { day }
}

public struct StatsWork: Codable, Hashable, Identifiable {
    public let title: String
    public let kind: String
    public let seconds: Double
    public var id: String { kind + ":" + title }
}

public struct StatsSummary: Codable {
    public let period: String
    public let totalSeconds: Double
    public let activeDays: Int
    public let sessions: Int
    public let streak: Int
    public let breakdown: [String: Double]
    public let days: [StatsDay]
    public let works: [StatsWork]
    /// Stats cards (longest streak, words met ...); absent from older Macs.
    public let highlights: StatsHighlights?
    /// The Mac's kept copy while it rebuilds a newer one (asked again a moment later); absent
    /// from older Macs.
    public let stale: Bool?
    /// When the Mac built this copy (seconds since 1970); absent from older Macs.
    public let builtAt: Double?
}

/// A day, a session or a work named in the fun stats (only the fields that apply are set).
public struct StatsEntry: Codable, Hashable {
    public let title: String?
    public let kind: String?
    public let day: String?
    public let seconds: Double?
}

public struct StatsWordCounts: Codable, Hashable {
    public let total: Double
    public let unique: Double
    public let new: Double
}

/// Words met in the period (estimates); nil totals while nothing could be counted yet.
public struct StatsWords: Codable, Hashable {
    public let total: Double?
    public let unique: Double?
    public let new: Double?
    /// Pieces of text the Mac is still parsing / that have no text (no subtitles or OCR yet).
    public let pending: Int
    public let missing: Int
    public let byKind: [String: StatsWordCounts]
}

public struct StatsHighlights: Codable, Hashable {
    public let longestStreakDays: Int
    public let busiestDay: StatsEntry?
    public let longestSession: StatsEntry?
    public let topWork: StatsEntry?
    public let animeEpisodes: Int
    public let lnCharactersPerMinute: Double?
    public let mangaPagesPerHour: Double?
    public let words: StatsWords?
}

public struct PlanningItem: Codable, Hashable, Identifiable {
    public let mediaId: Int
    public let title: String
    public let status: String
    public let progress: Int
    public let episodes: Int?
    public let nextEpisode: Int?
    public let nextAiringAt: Int64?
    public let readyEpisodes: Int
    public let score: Int?
    public var id: Int { mediaId }
}

public struct PlanningSummary: Codable {
    public let current: [PlanningItem]
    public let planned: [PlanningItem]
}
