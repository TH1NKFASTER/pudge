import SwiftUI
import PudgeCore

enum StudyStyle {
    /// Underline colour for a knowledge state (nil: no mark).
    static func mark(_ state: Int) -> Color? {
        switch state {
        case 0: return Color.orange.opacity(0.85)
        case 1: return Color.yellow.opacity(0.8)
        case 2: return Color.red.opacity(0.85)
        default: return nil
        }
    }
    static func label(_ state: Int) -> String {
        switch state {
        case 1: return "Learning"
        case 2: return "Due"
        case 3: return "Known"
        case 4: return "Ignored"
        default: return "New"
        }
    }
    static func tone(_ value: String) -> Color {
        switch value {
        case "again": return .red
        case "hard": return .orange
        case "easy": return .blue
        default: return PudgeTheme.good
        }
    }
    static func linkURL(group: Int, token: Int) -> URL { URL(string: "pudge-word://w/\(group)/\(token)")! }
    /// (group, token) from a tapped word link.
    static func parse(_ url: URL) -> (Int, Int)? {
        guard url.scheme == "pudge-word" else { return nil }
        let parts = url.pathComponents.filter { $0 != "/" }
        guard parts.count == 2, let group = Int(parts[0]), let token = Int(parts[1]) else { return nil }
        return (group, token)
    }
}

/// Japanese text whose words are links (`pudge-word://w/<group>/<token>`).
func studyText(_ text: String, tokens: [StudyToken], group: Int, highlight: Int? = nil,
               base: Color = .primary, marks: Bool = true, look: ReaderLook? = nil) -> AttributedString {
    var result = AttributedString()
    for segment in StudyText.segments(text, tokens: tokens) {
        var piece = AttributedString(segment.text)
        piece[AttributeScopes.SwiftUIAttributes.ForegroundColorAttribute.self] = base
        if let index = segment.token {
            let token = tokens[index]
            if token.hasEntry {
                piece[AttributeScopes.FoundationAttributes.LinkAttribute.self] = StudyStyle.linkURL(group: group, token: index)
                if marks, token.wantsMark, let color = look.map({ readerLook in readerLook.color(for: token).flatMap { UIColor(hex: $0) }.map(Color.init(uiColor:)) ?? base }) ?? StudyStyle.mark(token.state) {
                    piece[AttributeScopes.SwiftUIAttributes.UnderlineStyleAttribute.self] = Text.LineStyle(pattern: .solid, color: color)
                }
            }
            if index == highlight {
                piece[AttributeScopes.SwiftUIAttributes.BackgroundColorAttribute.self] = PudgeTheme.accent.opacity(0.45)
            }
        }
        result += piece
    }
    return result
}

/// Dictionary entry and the Mac-sent study actions for one word.
struct WordDetailView: View {
    @ObservedObject var media: MediaModel
    let lookup: WordLookup
    @State private var detail: WordDetail?
    @State private var failure: String?
    @State private var outcome: StudyActionResult?
    @State private var working = false
    @State private var deck = ""
    /// Shown from the data saved at the last update (the Mac did not answer).
    @State private var outdated = false
    @State private var online = true

    /// Without the Mac: the last known state (saved states are newer than the entry's own).
    private var state: Int {
        outdated ? (media.savedState(lookup.token)?.state ?? (lookup.token.state != 0 ? lookup.token.state : detail?.state ?? 0))
            : (detail?.state ?? lookup.token.state)
    }

    var body: some View {
        List {
            Section {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(detail?.spelling.isEmpty == false ? detail!.spelling : lookup.surface).font(.largeTitle.bold())
                    if let reading = detail?.reading, !reading.isEmpty, reading != detail?.spelling {
                        Text(reading).font(.title3).foregroundStyle(.secondary)
                    }
                }
                HStack {
                    Text(!outdated && detail?.stateLabel.isEmpty == false ? detail!.stateLabel : StudyStyle.label(state))
                        .font(.caption.bold()).padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill((StudyStyle.mark(state) ?? PudgeTheme.good).opacity(0.25)))
                    if let rank = detail?.frequencyRank { Text("Frequency #\(rank)").font(.caption).foregroundStyle(.secondary) }
                    if outdated {
                        Spacer()
                        Image(systemName: "clock.badge.exclamationmark").foregroundStyle(.orange)
                            .accessibilityLabel("Saved earlier, may be outdated")
                            .accessibilityIdentifier("word-outdated")
                    }
                }
                if outdated {
                    Text("Saved at the last update with the Mac · may be outdated").font(.caption).foregroundStyle(.secondary)
                }
                if let other = detail?.readings.filter({ $0 != detail?.reading && $0 != detail?.spelling }), !other.isEmpty {
                    Text(other.joined(separator: " ・ ")).font(.subheadline).foregroundStyle(.secondary)
                }
            }
            Section {
                if !online || outdated {
                    // Grades and decks change Jiten through the Mac: not without it.
                    Label("Grades and decks need the Mac. They come back when the \(PudgeTheme.device) reaches it.", systemImage: "wifi.slash")
                        .font(.caption).foregroundStyle(.secondary).accessibilityIdentifier("study-offline")
                } else if let options = media.options {
                    // The grades come first, right under the word: reachable without scrolling
                    // past the meanings.
                    HStack(spacing: 8) {
                        ForEach(options.grades) { grade in
                            Button(grade.label) { Task { await act("review", grade: grade.id) } }
                                .buttonStyle(.bordered).tint(StudyStyle.tone(grade.tone)).frame(maxWidth: .infinity)
                        }
                    }.disabled(working)
                    Picker("Deck", selection: $deck) {
                        Text("No deck").tag("")
                        ForEach(options.decks) { item in Text(item.name).tag(item.id) }
                    }
                    Button { Task { await act("add") } } label: { Label("Add to deck", systemImage: "plus.circle") }
                        .disabled(working || deck.isEmpty)
                    if !options.message.isEmpty { Text(options.message).font(.caption).foregroundStyle(.secondary) }
                } else {
                    ProgressView("Study options…")
                }
                if working { ProgressView("Sending through the Mac…") }
                if let outcome {
                    Label(outcome.ok ? "Saved" : outcome.message,
                          systemImage: outcome.ok ? "checkmark.circle.fill" : outcome.outcome == "unknown" ? "questionmark.circle" : "exclamationmark.triangle")
                        .foregroundStyle(outcome.ok ? PudgeTheme.good : .orange)
                }
            } header: { Text("Study") } footer: {
                Text("The Mac sends it to Jiten. If the answer is unknown, Pudge does not repeat it.")
            }
            Section("Meanings") {
                if let failure { Text(failure).foregroundStyle(.orange) }
                else if detail == nil { ProgressView() }
                else if detail!.meanings.isEmpty { Text(detail!.complete ? "No meanings returned" : "Loading the dictionary…").foregroundStyle(.secondary) }
                ForEach(Array((detail?.meanings ?? []).enumerated()), id: \.offset) { index, meaning in
                    Text("\(index + 1). \(meaning)")
                }
            }
            if !lookup.sentence.isEmpty {
                Section("Sentence") { Text(lookup.sentence).textSelection(.enabled) }
            }
            Section {
                if let play = lookup.playFromHere {
                    Button { play() } label: { Label(lookup.playLabel, systemImage: "play.circle") }
                }
                if lookup.token.wordId > 0, let url = URL(string: "https://jiten.moe/vocabulary/\(lookup.token.wordId)/\(lookup.token.readingIndex)") {
                    Link(destination: url) { Label("Open in Jiten", systemImage: "safari") }
                }
            }
        }
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        .navigationTitle(lookup.surface).navigationBarTitleDisplayMode(.inline)
        .task {
            online = !media.app.macOffline
            await media.loadOptions()
            deck = media.deckOverride ?? media.options?.deckId ?? ""
            await load()
            if detail?.complete == false && !outdated {
                try? await Task.sleep(nanoseconds: 1_200_000_000)
                await load(refresh: true)
            }
        }
        .onReceive(media.app.$macAvailable) { available in
            // The new value (macOffline still has the old one here); back online: fresh data and grades.
            let next = available || media.app.paired == nil || !media.app.macChecked
            guard next != online else { return }
            online = next
            if next && (outdated || failure != nil) {
                Task {
                    await media.loadOptions()
                    deck = media.deckOverride ?? media.options?.deckId ?? ""
                    await load(refresh: true)
                }
            }
        }
        .onChange(of: deck) { _, value in
            if value != (media.deckOverride ?? media.options?.deckId ?? "") { Task { await media.saveDeck(value) } }
        }
    }

    private func load(refresh: Bool = false) async {
        // Without the Mac: the entry saved at the last update, no waiting for the network.
        if media.app.macOffline { showSaved(); return }
        do {
            detail = try await media.word(lookup.token, surface: lookup.surface, refresh: refresh)
            outdated = false; failure = nil
        } catch {
            if media.savedWord(lookup.token) != nil || media.app.macOffline { showSaved() }
            else { failure = error.localizedDescription }
        }
    }

    private func showSaved() {
        if let saved = media.savedWord(lookup.token) { detail = saved; outdated = true; failure = nil }
        else { failure = "This word was not saved on the \(PudgeTheme.device) yet. Its meanings appear when the \(PudgeTheme.device) reaches the Mac." }
    }

    private func act(_ action: String, grade: String? = nil) async {
        guard !working, online, !outdated else { return }
        working = true; outcome = nil
        outcome = await media.study(action, grade: grade, token: lookup.token, sentence: lookup.sentence, deck: deck)
        working = false
        if outcome?.ok == true { await load(refresh: true) }
    }
}

struct WordSheet: View {
    @ObservedObject var media: MediaModel
    let lookup: WordLookup
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            WordDetailView(media: media, lookup: lookup)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
        .tint(PudgeTheme.accent).preferredColorScheme(.dark)
    }
}

/// Splits any text into words on the Mac (manga bubbles, plain subtitles).
struct TextLookupSheet: View {
    @ObservedObject var media: MediaModel
    let lookup: TextLookup
    @Environment(\.dismiss) private var dismiss
    @State private var parsed: ParsedText?
    @State private var failure: String?
    @State private var selected: WordLookup?
    @AppStorage("pudge.readerFontSize") private var fontSize = 21.0
    @AppStorage("pudge.readerMarkStyle") private var marks = "mac"
    @AppStorage("pudge.readerFurigana") private var furigana = "mac"
    var body: some View {
        let style = ReaderStyle.make(media.readerLook, fontSize: fontSize, marks: marks, furigana: furigana)
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let parsed {
                        ForEach(Array(parsed.paragraphs.enumerated()), id: \.offset) { _, paragraph in
                            JapaneseText(paragraph: paragraph, style: style, onTap: { index in
                                guard let token = paragraph.tokens[safe: index] else { return }
                                let surface = StudyText.segments(paragraph.text, tokens: paragraph.tokens).first { $0.token == index }?.text ?? ""
                                selected = WordLookup(token: token, surface: surface, sentence: StudyText.sentence(paragraph.text, around: token))
                            })
                        }
                        Text("Tap a word").font(.caption).foregroundStyle(.secondary)
                    } else if let failure {
                        Text(lookup.text).font(.system(size: fontSize)).textSelection(.enabled)
                        Text(failure).foregroundStyle(.orange)
                    } else {
                        Text(lookup.text).font(.system(size: fontSize)).foregroundStyle(.secondary)
                        ProgressView()
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(20)
            }
            .background(PudgeTheme.background)
            .navigationDestination(item: $selected) { item in WordDetailView(media: media, lookup: item) }
            .navigationTitle("Look up").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .task {
                let look = Task { await media.loadReaderLook() }
                if media.readerLook == nil { await look.value }
                do { parsed = try await media.parse(lookup.text) } catch { failure = error.localizedDescription }
            }
        }
        .presentationDetents([.medium, .large])
        .tint(PudgeTheme.accent).preferredColorScheme(.dark)
    }
}

/// Cover image served by the Mac (cached on the iPhone).
struct RemoteCover: View {
    @ObservedObject var media: MediaModel
    let path: String
    var width: CGFloat = 54
    var symbol = "book.closed"
    @State private var image: UIImage?
    var body: some View {
        Group {
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { ZStack { PudgeTheme.panel; Image(systemName: symbol).font(.system(size: width * 0.36)).foregroundStyle(PudgeTheme.accent) } }
        }
        .frame(width: width, height: width * 1.4).clipShape(RoundedRectangle(cornerRadius: 8)).accessibilityHidden(true)
        .task(id: path) { image = await media.image(path) }
    }
}

extension Array {
    subscript(safe index: Int) -> Element? { indices.contains(index) ? self[index] : nil }
}

enum PudgeFormat {
    static func text(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        if total >= 3600 { return "\(total / 3600) h \(total / 60 % 60) min" }
        if total >= 60 { return "\(total / 60) min" }
        return "\(total) s"
    }
}
