import Foundation

/// The Markdown of an LLM answer in a chat (grammar chat): its blocks for the app to draw. Fenced
/// code (``` or ~~~), headings, bullet and numbered lists, quotes, rules and paragraphs; the text of
/// each block keeps its inline Markdown (bold, italics, `code`, links) for `ChatMarkdown.inline`.
/// A partial answer works too: a fence not closed yet is code up to the end (`closed` false).
/// Nothing here is HTML: tags stay text.
public enum ChatBlock: Hashable, Sendable {
    case paragraph(String)
    case heading(level: Int, text: String)
    case code(language: String, text: String, closed: Bool)
    case list(ordered: Bool, items: [ChatListItem])
    case quote(String)
    case rule
}

public struct ChatListItem: Hashable, Sendable {
    /// "•" for a bullet, "3." for a numbered item (its own number).
    public var marker: String
    public var text: String
    /// Nesting level (0 at the left; 2 spaces or a tab per level).
    public var level: Int
    public init(marker: String, text: String, level: Int = 0) { self.marker = marker; self.text = text; self.level = level }
}

public enum ChatMarkdown {
    public static func blocks(_ source: String) -> [ChatBlock] {
        let lines = source.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
        var blocks: [ChatBlock] = []
        var paragraph: [String] = []
        var quote: [String] = []
        var items: [ChatListItem] = []
        var ordered = false
        /// A blank line inside a list: the list goes on only with another item.
        var listGap = false

        func flushParagraph() {
            let text = paragraph.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { blocks.append(.paragraph(text)) }
            paragraph = []
        }
        func flushQuote() {
            let text = quote.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { blocks.append(.quote(text)) }
            quote = []
        }
        func flushList() {
            if !items.isEmpty { blocks.append(.list(ordered: ordered, items: items)) }
            items = []
            listGap = false
        }
        func flushAll() { flushParagraph(); flushQuote(); flushList() }

        var index = 0
        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if let fence = Fence(line) {
                flushAll()
                var body: [String] = []
                var closed = false
                index += 1
                while index < lines.count {
                    if fence.closes(lines[index]) { closed = true; break }
                    body.append(fence.strip(lines[index]))
                    index += 1
                }
                blocks.append(.code(language: fence.language, text: body.joined(separator: "\n"), closed: closed))
                index += 1
                continue
            }
            if trimmed.isEmpty {
                flushParagraph(); flushQuote()
                if !items.isEmpty { listGap = true }
                index += 1
                continue
            }
            if let found = Self.heading(trimmed), Self.leadingSpaces(line) < 4 {
                flushAll()
                blocks.append(.heading(level: found.level, text: found.text))
            } else if Self.isRule(trimmed), Self.leadingSpaces(line) < 4 {
                flushAll()
                blocks.append(.rule)
            } else if trimmed.hasPrefix(">") {
                flushParagraph(); flushList()
                var text = String(trimmed.dropFirst())
                if text.hasPrefix(" ") { text.removeFirst() }
                quote.append(text)
            } else if let item = Self.listItem(line) {
                flushParagraph(); flushQuote()
                if !items.isEmpty, item.ordered != ordered, item.item.level == 0 { flushList() }
                if items.isEmpty { ordered = item.ordered }
                items.append(item.item)
                listGap = false
            } else if !items.isEmpty, !listGap || Self.leadingSpaces(line) >= 2 {
                // A line of the item above (indented, or right under it).
                items[items.count - 1].text += "\n" + trimmed
                listGap = false
            } else if !quote.isEmpty {
                quote.append(trimmed)
            } else {
                flushList()
                paragraph.append(line)
            }
            index += 1
        }
        flushAll()
        return blocks
    }

    /// A block's text with its inline Markdown (bold, italics, `code`, strikethrough, links)
    /// interpreted, line breaks kept. Untrusted model text: only web links (http, https) keep their
    /// target; other links (javascript:, file:, app schemes) and images lose theirs (their text
    /// stays; nothing is loaded).
    public static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard var value = try? AttributedString(markdown: text, options: options) else { return AttributedString(text) }
        let ranges = value.runs.filter { $0.link != nil || $0.imageURL != nil }.map { ($0.range, $0.link) }
        for (range, link) in ranges {
            value[range].imageURL = nil
            if let link, !webLink(link) { value[range].link = nil }
        }
        return value
    }

    public static func webLink(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return false }
        return url.host?.isEmpty == false
    }

    // MARK: lines

    struct Fence {
        let character: Character
        let count: Int
        let indent: Int
        let language: String
        init?(_ line: String) {
            let indent = ChatMarkdown.leadingSpaces(line)
            guard indent < 4 else { return nil }
            let rest = line.drop { $0 == " " }
            guard let first = rest.first, first == "`" || first == "~" else { return nil }
            let run = rest.prefix { $0 == first }.count
            guard run >= 3 else { return nil }
            let info = rest.dropFirst(run).trimmingCharacters(in: .whitespaces)
            // A backtick fence's info cannot hold a backtick (that is inline code).
            if first == "`", info.contains("`") { return nil }
            character = first; count = run; self.indent = indent
            language = String(info.split(separator: " ").first ?? "")
        }
        func closes(_ line: String) -> Bool {
            guard ChatMarkdown.leadingSpaces(line) < 4 else { return false }
            let rest = line.trimmingCharacters(in: .whitespaces)
            return rest.count >= count && rest.allSatisfy { $0 == character }
        }
        func strip(_ line: String) -> String {
            var dropped = 0
            var value = Substring(line)
            while dropped < indent, value.first == " " { value = value.dropFirst(); dropped += 1 }
            return String(value)
        }
    }

    static func leadingSpaces(_ line: String) -> Int {
        var count = 0
        for character in line {
            if character == " " { count += 1 } else if character == "\t" { count += 4 } else { break }
        }
        return count
    }

    static func heading(_ trimmed: String) -> (level: Int, text: String)? {
        let level = trimmed.prefix { $0 == "#" }.count
        guard (1...6).contains(level) else { return nil }
        let rest = trimmed.dropFirst(level)
        guard rest.isEmpty || rest.first == " " || rest.first == "\t" else { return nil }
        var text = rest.trimmingCharacters(in: .whitespaces)
        // A closing run of #s goes ("## Title ##").
        while text.hasSuffix("#") { text.removeLast() }
        return (level, text.trimmingCharacters(in: .whitespaces))
    }

    static func isRule(_ trimmed: String) -> Bool {
        let compact = trimmed.filter { $0 != " " }
        guard compact.count >= 3, let first = compact.first, "-*_".contains(first) else { return false }
        return compact.allSatisfy { $0 == first }
    }

    static func listItem(_ line: String) -> (ordered: Bool, item: ChatListItem)? {
        let spaces = leadingSpaces(line)
        let rest = line.drop { $0 == " " || $0 == "\t" }
        let level = min(6, spaces / 2)
        if let first = rest.first, "-*+".contains(first) {
            let after = rest.dropFirst()
            guard after.first == " " || after.first == "\t" else { return nil }
            return (false, ChatListItem(marker: "•", text: after.trimmingCharacters(in: .whitespaces), level: level))
        }
        let digits = rest.prefix { $0.isASCII && $0.isNumber }
        guard (1...9).contains(digits.count) else { return nil }
        let after = rest.dropFirst(digits.count)
        guard let mark = after.first, mark == "." || mark == ")" else { return nil }
        let text = after.dropFirst()
        guard text.first == " " || text.first == "\t" else { return nil }
        return (true, ChatListItem(marker: String(digits) + ".", text: text.trimmingCharacters(in: .whitespaces), level: level))
    }
}
