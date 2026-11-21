import Foundation

/// "Explain grammar" on the iPhone/iPad (the light-novel reader, manga bubbles): a chat with
/// the Mac's LLM about one sentence (`media/grammar/…`, pudge/iphone_grammar.py). The Mac asks
/// its LLM as its own reader does; the chats are kept on this device only, per book.

/// What a chat explains: the selected text (a sentence, a manga bubble) and the text before it.
public struct GrammarSource: Codable, Hashable, Sendable {
    public var sentence: String
    public var context: String
    public init(sentence: String, context: String = "") { self.sentence = sentence; self.context = context }
}

/// The Mac's limits (pudge/iphone_grammar.py; longer is refused there) and what is kept here.
public enum GrammarLimits {
    /// Characters (Unicode scalars, as the Mac counts) of a sentence and of a question.
    public static let sentence = 2000
    public static let question = 4000
    /// Text before a reader's sentence sent with it (the Mac's reader sends the same 200).
    public static let readerContext = 200
    /// A manga page's text boxes sent with a bubble.
    public static let pageContext = 1500
    /// Messages of a chat sent with a question, and characters of each (the Mac keeps 20 of 4000).
    public static let turns = 20
    public static let turnCharacters = 4000
    /// Chats kept per book and in all, messages kept per chat.
    public static let chatsPerBook = 5
    public static let chats = 100
    public static let messages = 60

    /// `text` cut to `limit` Unicode scalars.
    public static func clip(_ text: String, _ limit: Int) -> String {
        let scalars = text.unicodeScalars
        guard scalars.count > limit else { return text }
        var view = String.UnicodeScalarView()
        view.append(contentsOf: scalars.prefix(max(0, limit)))
        return String(view)
    }
}

public enum GrammarContext {
    /// The text before `position` in a reader's chapter: the start of its paragraph and the
    /// paragraphs before it (illustrations left out), joined by line breaks, its last `limit`
    /// characters (the Mac's reader sends the 200 before the selection).
    public static func preceding(_ paragraph: (Int) -> TextParagraph?, before position: TextPosition,
                                 limit: Int = GrammarLimits.readerContext) -> String {
        guard limit > 0 else { return "" }
        var parts: [String] = []
        var count = 0
        var index = position.paragraph
        while index >= 0, count < limit, position.paragraph - index <= 50 {
            if let item = paragraph(index), !item.image {
                let units = Array(item.text.utf16)
                let end = index == position.paragraph ? min(max(0, position.offset), units.count) : units.count
                let text = String(decoding: units[0..<end], as: UTF16.self).trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty {
                    parts.insert(text, at: 0)
                    count += text.count + 1
                }
            }
            index -= 1
        }
        return tail(parts.joined(separator: "\n"), limit)
    }

    /// A manga page's text boxes in order as the context of one of them, the last `limit` characters.
    public static func joined(_ texts: [String], limit: Int = GrammarLimits.pageContext) -> String {
        let parts = texts.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        return tail(parts.joined(separator: "\n"), limit)
    }

    static func tail(_ text: String, _ limit: Int) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count > limit else { return trimmed }
        return String(trimmed.suffix(max(0, limit))).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// One grammar point of an explanation (plain texts: nothing in them is rendered as markup).
public struct GrammarPoint: Codable, Hashable, Sendable {
    public var pattern: String
    public var meaning: String
    public var explanation: String
    public var form: String
    enum CodingKeys: String, CodingKey { case pattern, meaning, explanation, form }
    public init(pattern: String, meaning: String = "", explanation: String = "", form: String = "") {
        self.pattern = pattern; self.meaning = meaning; self.explanation = explanation; self.form = form
    }
    public init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        pattern = (try? row.decodeIfPresent(String.self, forKey: .pattern)) ?? ""
        meaning = (try? row.decodeIfPresent(String.self, forKey: .meaning)) ?? ""
        explanation = (try? row.decodeIfPresent(String.self, forKey: .explanation)) ?? ""
        form = (try? row.decodeIfPresent(String.self, forKey: .form)) ?? ""
    }
}

/// The Mac's explanation of a sentence: its translation and grammar points; `summary` is what
/// the Mac's assistant gives the LLM as this first answer (in the Mac's language).
public struct GrammarExplanation: Codable, Hashable, Sendable {
    public var translation: String
    public var points: [GrammarPoint]
    /// Some points could not be placed in the sentence (the Mac shows them anyway).
    public var partial: Bool
    public var summary: String
    enum CodingKeys: String, CodingKey { case translation, points, partial, summary }
    public init(translation: String, points: [GrammarPoint], partial: Bool = false, summary: String = "") {
        self.translation = translation; self.points = points; self.partial = partial; self.summary = summary
    }
    public init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: CodingKeys.self)
        translation = (try? row.decodeIfPresent(String.self, forKey: .translation)) ?? ""
        points = ((try? row.decodeIfPresent([GrammarPoint].self, forKey: .points)) ?? []).filter { !$0.pattern.isEmpty }
        partial = (try? row.decodeIfPresent(Bool.self, forKey: .partial)) ?? false
        summary = (try? row.decodeIfPresent(String.self, forKey: .summary)) ?? ""
    }
}

/// `POST media/grammar/explain`, `POST media/grammar/ask` and `GET media/grammar/jobs/<job>?wait=1`:
/// `state` "running" (ask for `job` again), "done" (`explanation` or `reply`) or "failed"
/// (`error`: the readable reason).
public struct GrammarJob: Decodable, Sendable {
    public let job: String?
    public let state: String
    public let explanation: GrammarExplanation?
    public let reply: String?
    public let error: String?
    public let detail: String?
    public var running: Bool { state == "running" }
    public init(job: String?, state: String, explanation: GrammarExplanation? = nil, reply: String? = nil,
                error: String? = nil, detail: String? = nil) {
        self.job = job; self.state = state; self.explanation = explanation; self.reply = reply
        self.error = error; self.detail = detail
    }
}

public struct GrammarExplainRequest: Codable, Sendable {
    public let sentence: String
    public let context: String
    public init(_ source: GrammarSource) { sentence = source.sentence; context = source.context }
}

/// A message of the chat as the Mac's LLM gets it.
public struct GrammarTurn: Codable, Hashable, Sendable {
    public let role: String
    public let content: String
    public init(role: String, content: String) { self.role = role; self.content = content }
}

public struct GrammarPointRequest: Codable, Hashable, Sendable {
    public let pattern: String
    public let meaning: String
    public init(pattern: String, meaning: String) { self.pattern = pattern; self.meaning = meaning }
}

/// A question with its chat: the sentence, its context, the points already shown and the
/// messages so far (the last one is the question).
public struct GrammarAskRequest: Codable, Sendable {
    public let sentence: String
    public let context: String
    public let points: [GrammarPointRequest]
    public let messages: [GrammarTurn]
}

public struct GrammarChatMessage: Codable, Hashable, Identifiable, Sendable {
    public enum Role: String, Codable, Sendable { case user, assistant }
    public var id: UUID
    public var role: Role
    /// What the chat shows: a question or an answer (plain text, an answer's simple Markdown).
    public var text: String
    /// The first answer: the explanation of the sentence.
    public var explanation: GrammarExplanation?
    public var date: Date
    public init(role: Role, text: String, explanation: GrammarExplanation? = nil, id: UUID = UUID(), date: Date = Date()) {
        self.id = id; self.role = role; self.text = text; self.explanation = explanation; self.date = date
    }
    /// What the LLM gets back of it: an explanation as the Mac's assistant sums it up, otherwise the text.
    public var modelText: String {
        if let summary = explanation?.summary, !summary.isEmpty { return summary }
        return text
    }
}

/// A chat about one sentence of a book: the request and the explanation first, then questions
/// and answers.
public struct GrammarChat: Codable, Hashable, Identifiable, Sendable {
    public var id: UUID
    /// The book it belongs to (`GrammarChat.book(server:kind:id:)`).
    public var book: String
    public var source: GrammarSource
    public var messages: [GrammarChatMessage]
    public var createdAt: Date
    public var updatedAt: Date

    public init(book: String, source: GrammarSource, id: UUID = UUID(), date: Date = Date()) {
        self.id = id; self.book = book; self.source = source; messages = []; createdAt = date; updatedAt = date
    }

    /// A book of a Mac (each Mac numbers its books from 1): "<server>/<kind>/<id>".
    public static func book(server: String?, kind: String, id: Int) -> String { "\(server ?? "local")/\(kind)/\(id)" }

    /// The chat's first message, what it explains.
    public static func request(_ sentence: String) -> String { "Explain the grammar of «\(sentence)»" }

    /// The explanation of the sentence (the first answer), nil until it came.
    public var explanation: GrammarExplanation? { messages.first { $0.explanation != nil }?.explanation }
    public var explained: Bool { explanation != nil }
    /// The last question has no answer (it failed or the app closed meanwhile): Try again sends it again.
    public var waitingForAnswer: Bool { messages.last?.role == .user }

    /// A message added; past `limit` the oldest questions and answers go (the request and the
    /// explanation stay).
    public mutating func append(_ message: GrammarChatMessage, now: Date = Date(), limit: Int = GrammarLimits.messages) {
        messages.append(message)
        let keep = max(2, limit)
        if messages.count > keep {
            messages = Array(messages.prefix(2)) + Array(messages.dropFirst(2).suffix(keep - 2))
        }
        updatedAt = now
    }

    /// The conversation for the Mac's LLM: questions and answers in order, the newest `limit`,
    /// each cut to `characters`.
    public func turns(limit: Int = GrammarLimits.turns, characters: Int = GrammarLimits.turnCharacters) -> [GrammarTurn] {
        let rows = messages
            .map { GrammarTurn(role: $0.role.rawValue, content: GrammarLimits.clip($0.modelText, characters)) }
            .filter { !$0.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        return Array(rows.suffix(max(0, limit)))
    }

    /// The last question with its chat, for `media/grammar/ask`.
    public var askRequest: GrammarAskRequest {
        GrammarAskRequest(sentence: source.sentence, context: source.context,
                          points: (explanation?.points ?? []).prefix(30).map { GrammarPointRequest(pattern: $0.pattern, meaning: $0.meaning) },
                          messages: turns())
    }
}

/// The chats kept on this device: per book the `perBook` used last, at most `total` in all (the
/// ones used least recently go first). Nothing of them is sent to the Mac except with a question.
public struct GrammarChatArchive: Codable, Hashable, Sendable {
    public var chats: [GrammarChat]
    public init(chats: [GrammarChat] = []) { self.chats = chats }

    /// A book's chats, the one used last first.
    public func chats(book: String) -> [GrammarChat] {
        chats.filter { $0.book == book }.sorted { $0.updatedAt > $1.updatedAt }
    }
    public func latest(book: String) -> GrammarChat? { chats(book: book).first }

    public mutating func keep(_ chat: GrammarChat, perBook: Int = GrammarLimits.chatsPerBook, total: Int = GrammarLimits.chats) {
        chats.removeAll { $0.id == chat.id }
        chats.append(chat)
        chats.sort { $0.updatedAt > $1.updatedAt }
        var counts: [String: Int] = [:]
        chats = chats.filter { item in
            counts[item.book, default: 0] += 1
            return (counts[item.book] ?? 0) <= perBook
        }
        if chats.count > total { chats = Array(chats.prefix(max(0, total))) }
    }

    public mutating func remove(_ id: UUID) { chats.removeAll { $0.id == id } }
}

/// The archive in one JSON file (Application Support/PudgeAudio/grammar-chats.json), written whole.
public final class GrammarChatStore {
    public let file: URL
    public private(set) var archive: GrammarChatArchive

    public init(file: URL) {
        self.file = file
        if let data = try? Data(contentsOf: file), let value = try? JSONDecoder().decode(GrammarChatArchive.self, from: data) {
            archive = value
        } else {
            archive = GrammarChatArchive()
        }
    }

    public func save(_ chat: GrammarChat) { archive.keep(chat); write() }
    public func remove(_ id: UUID) { archive.remove(id); write() }

    private func write() {
        guard let data = try? JSONEncoder().encode(archive) else { return }
        try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: file, options: .atomic)
    }
}

public enum GrammarText {
    /// An LLM answer for display: its simple Markdown (bold, italics, `code`) shown as such and
    /// nothing else interpreted. Model and book text are untrusted: links and images lose their
    /// targets (a tap opens nothing, nothing is loaded); no HTML is ever rendered.
    public static func answer(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard var value = try? AttributedString(markdown: text, options: options) else { return AttributedString(text) }
        let linked = value.runs.filter { $0.link != nil || $0.imageURL != nil }.map(\.range)
        for range in linked {
            value[range].link = nil
            value[range].imageURL = nil
        }
        return value
    }
}
