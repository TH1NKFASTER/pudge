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

/// Dictionary entry and the Mac-sent study actions for one word. Shown in a BottomPanel: its top
/// (the word, its reading, the first meanings, the grades and the deck) fits the compact panel.
struct WordDetailView: View {
    @ObservedObject var media: MediaModel
    let lookup: WordLookup
    /// "Explain grammar" of the word's sentence (the light-novel reader, manga); nil elsewhere.
    var onExplain: (() -> Void)? = nil
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
                    Text(detail?.spelling.isEmpty == false ? detail!.spelling : lookup.surface).font(JapaneseType.font(.largeTitle, bold: true))
                    if let reading = detail?.reading, !reading.isEmpty, reading != detail?.spelling {
                        Text(reading).font(JapaneseType.font(.title3)).foregroundStyle(.secondary)
                    }
                }
                HStack {
                    Text(WordStateLabel.text(state: state, macLabel: outdated ? "" : detail?.stateLabel ?? "", fallback: StudyStyle.label))
                        .font(.caption.bold()).padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill((StudyStyle.mark(state) ?? PudgeTheme.good).opacity(0.25)))
                    // Jiten's frequency rank next to the state, as on the Mac (none when unknown).
                    if let frequency = WordFrequency.label(detail?.frequencyRank) {
                        Text(frequency).font(.caption).foregroundStyle(.secondary).accessibilityIdentifier("word-frequency")
                    }
                    Spacer()
                    if outdated {
                        Image(systemName: "clock.badge.exclamationmark").foregroundStyle(.orange)
                            .accessibilityLabel("Saved earlier, may be outdated")
                            .accessibilityIdentifier("word-outdated")
                    }
                    if let onExplain {
                        Button { onExplain() } label: { Image(systemName: "text.bubble") }
                            .buttonStyle(.borderless)
                            .accessibilityLabel("Explain grammar of the sentence")
                    }
                }
                if outdated {
                    Text("Saved at the last update with the Mac · may be outdated").font(.caption).foregroundStyle(.secondary)
                }
                if let other = detail?.readings.filter({ $0 != detail?.reading && $0 != detail?.spelling }), !other.isEmpty {
                    Text(other.joined(separator: " ・ ")).font(JapaneseType.font(.subheadline)).foregroundStyle(.secondary)
                }
                // The first meanings, so the compact panel shows them with the grades (all of
                // them are under Meanings).
                if let first = detail?.meanings.prefix(2), !first.isEmpty {
                    Text(first.joined(separator: " · ")).font(.callout).lineLimit(2)
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
                    // The deck and Add on one row: both fit the compact panel.
                    HStack {
                        Picker("Deck", selection: $deck) {
                            Text("No deck").tag("")
                            ForEach(options.decks) { item in Text(item.name).tag(item.id) }
                        }
                        .labelsHidden().pickerStyle(.menu)
                        Spacer(minLength: 8)
                        Button { Task { await act("add") } } label: { Label("Add to deck", systemImage: "plus.circle") }
                            .buttonStyle(.bordered)
                            .disabled(working || deck.isEmpty)
                    }
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
                Section("Sentence") {
                    Text(lookup.sentence).textSelection(.enabled)
                    if let onExplain {
                        Button { onExplain() } label: { Label("Explain grammar", systemImage: "text.bubble") }
                    }
                }
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
        // In a BottomPanel: the panel's colour behind, compact rows, no navigation title (the panel
        // sits in the reader's navigation, whose title it would replace).
        .scrollContentBackground(.hidden)
        // The word, its readings, meanings and sentence: Japanese forms of the kanji.
        .japaneseTypesetting()
        .listSectionSpacing(.compact)
        .environment(\.defaultMinListRowHeight, 36)
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

/// The word card in a BottomPanel (`.bottomPanel(item:)`: the reader, subtitles, manga, the
/// read-along): it opens small with the word, its reading, first meanings and the grades, what
/// is behind stays visible; dragged up it shows everything.
struct WordSheet: View {
    @ObservedObject var media: MediaModel
    let lookup: WordLookup
    /// "Explain grammar" of the word's sentence (the light-novel reader, manga).
    var onExplain: (() -> Void)? = nil
    var body: some View {
        WordDetailView(media: media, lookup: lookup, onExplain: onExplain)
            .tint(PudgeTheme.accent)
    }
}

/// Splits any text into words on the Mac (manga bubbles, plain subtitles), in a BottomPanel; a
/// tapped word opens in the same panel (Back returns to the text).
struct TextLookupSheet: View {
    @ObservedObject var media: MediaModel
    let lookup: TextLookup
    /// "Explain grammar" (manga): the text to explain, the whole bubble or a word's sentence in it.
    var onExplain: ((String) -> Void)? = nil
    @State private var parsed: ParsedText?
    @State private var failure: String?
    @State private var selected: WordLookup?
    @AppStorage("pudge.readerFontSize") private var fontSize = 21.0
    @AppStorage("pudge.readerMarkStyle") private var marks = "mac"
    @AppStorage("pudge.readerFurigana") private var furigana = "mac"
    var body: some View {
        let style = ReaderStyle.make(media.readerLook, fontSize: fontSize, marks: marks, furigana: furigana)
        // A VStack, not a Group: the task below runs once, not again for each page of the panel.
        VStack(alignment: .leading, spacing: 0) {
            if let selected {
                Button { self.selected = nil } label: { Label("Back to the text", systemImage: "chevron.left") }
                    .padding(.horizontal, 16).padding(.vertical, 6)
                WordDetailView(media: media, lookup: selected, onExplain: explainAction(selected.sentence))
            } else {
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
                        if let explain = explainAction(lookup.text) {
                            Button { explain() } label: { Label("Explain grammar", systemImage: "text.bubble") }
                                .buttonStyle(.bordered)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 20).padding(.vertical, 12)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .tint(PudgeTheme.accent)
        .japaneseTypesetting()
        // A word graded here (or elsewhere): its occurrences in this text take the new state.
        .onChange(of: media.gradedWords) { _, graded in
            if let value = parsed?.updating(graded.rows) { parsed = value }
        }
        .task {
            let look = Task { await media.loadReaderLook() }
            if media.readerLook == nil { await look.value }
            do { parsed = try await media.parse(lookup.text) } catch { failure = error.localizedDescription }
        }
    }

    private func explainAction(_ text: String) -> (() -> Void)? {
        guard let onExplain else { return nil }
        return { onExplain(text) }
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

// MARK: Bottom panel

enum PanelMotion {
    static let spring = Animation.spring(response: 0.32, dampingFraction: 0.88)
}

/// A panel at the bottom of a screen (the word card, Look up, the grammar chat) instead of a
/// sheet: it opens compact over the lower part only, so what is behind stays visible (the video
/// stays in view). A tap outside the word card or Look up only closes it (see
/// BottomPanelPresenter); around the grammar chat what is behind stays usable. Dragged up by its
/// top (or with the chevron) it takes most of the height; dragged down it gets compact again,
/// then closes. On a wide screen (iPad, iPhone in landscape) it is a card at the bottom trailing
/// corner about as wide as a phone; on a narrow one it spans the width. (A sheet covers the
/// whole screen in landscape and is a large card in the middle of an iPad.)
struct BottomPanel<Content: View>: View {
    let title: String
    /// The compact height (less on a short screen).
    let compact: CGFloat
    /// The space it is shown in (safe areas and the keyboard left out).
    let space: CGSize
    @Binding var expanded: Bool
    let onClose: () -> Void
    @ViewBuilder let content: () -> Content
    @GestureState(resetTransaction: Transaction(animation: PanelMotion.spring)) private var pull: CGFloat = 0

    private var wide: Bool { space.width >= 600 }
    private var width: CGFloat { wide ? min(440, max(360, space.width * 0.42)) : space.width }
    /// The tallest it gets: a strip of the screen stays visible above it.
    private var tallest: CGFloat { max(140, space.height - (wide ? 24 : 40)) }
    /// Compact: at most 55% of a short space (an iPhone in landscape), so the video and its
    /// subtitles stay in view.
    private var small: CGFloat { min(compact, tallest, space.height < 500 ? space.height * 0.55 : .infinity) }
    private var resting: CGFloat { expanded ? tallest : small }
    private var height: CGFloat { min(tallest, max(110, resting - pull)) }
    private var shape: UnevenRoundedRectangle {
        UnevenRoundedRectangle(topLeadingRadius: 16, bottomLeadingRadius: wide ? 16 : 0,
                               bottomTrailingRadius: wide ? 16 : 0, topTrailingRadius: 16, style: .continuous)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            content()
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .frame(width: width, height: height)
        .clipShape(shape)
        .background {
            // Attached to the bottom edge, its colour goes on under the home indicator.
            shape.fill(PudgeTheme.panel)
                .overlay { shape.stroke(Color.white.opacity(0.08), lineWidth: 1) }
                .shadow(color: .black.opacity(0.45), radius: 14, y: 2)
                .ignoresSafeArea(.container, edges: wide ? Edge.Set() : .bottom)
        }
        .accessibilityAction(.escape) { onClose() }
        .japaneseTypesetting()
        .padding(wide ? 12 : 0)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: wide ? .bottomTrailing : .bottom)
        .environment(\.colorScheme, .dark)
    }

    private var header: some View {
        VStack(spacing: 4) {
            Capsule().fill(Color.secondary.opacity(0.6)).frame(width: 36, height: 5).padding(.top, 6)
                .accessibilityHidden(true)
            HStack(spacing: 4) {
                Text(title).font(.headline).lineLimit(1)
                Spacer(minLength: 8)
                Button { withAnimation(PanelMotion.spring) { expanded.toggle() } } label: {
                    Image(systemName: expanded ? "chevron.down" : "chevron.up").font(.subheadline.weight(.semibold))
                        .frame(width: 36, height: 30)
                }
                .accessibilityLabel(expanded ? "Make smaller" : "Make larger")
                Button { onClose() } label: {
                    Image(systemName: "xmark.circle.fill").font(.title3).symbolRenderingMode(.hierarchical)
                        .foregroundStyle(.secondary).frame(width: 36, height: 30)
                }
                .accessibilityLabel("Close")
            }
            .padding(.horizontal, 12)
        }
        .padding(.bottom, 4)
        .frame(maxWidth: .infinity)
        .contentShape(Rectangle())
        .gesture(drag)
    }

    /// Up: large; down: compact, further down (or flicked): closed.
    private var drag: some Gesture {
        DragGesture(minimumDistance: 8, coordinateSpace: .global)
            .updating($pull) { value, state, _ in state = value.translation.height }
            .onEnded { value in
                let end = resting - value.predictedEndTranslation.height
                withAnimation(PanelMotion.spring) {
                    if end < small * 0.6 { onClose() } else { expanded = end > (small + tallest) / 2 }
                }
            }
    }
}

private enum PanelShown: Identifiable {
    case shown
    var id: Int { 0 }
}

/// Shows `item` in a BottomPanel over the view (see `bottomPanel(item:)`).
private struct BottomPanelPresenter<Item: Identifiable, Panel: View>: ViewModifier {
    @Binding var item: Item?
    let title: String
    let compact: CGFloat
    let onDismiss: (() -> Void)?
    /// The panel has a text field (the grammar chat): typing moves only the panel above the
    /// keyboard, the reader or manga page behind keeps its size and place. Only that panel's
    /// presenter does it: around another panel it would keep this one under the keyboard.
    var keyboard = false
    /// A tap outside the panel only closes it: it opens no other word, turns no page and does
    /// not show or hide the video's controls (the word card, Look up). Off for the grammar
    /// chat: it comes back with its button, and the text behind stays usable while it is open.
    var closesOnOutsideTap = true
    let panel: (Item) -> Panel
    @State private var expanded = false

    func body(content: Content) -> some View {
        content
            .ignoresSafeArea(.keyboard, edges: keyboard ? .bottom : Edge.Set())
            .overlay {
                GeometryReader { space in
                    if let current = item {
                        ZStack {
                            if closesOnOutsideTap {
                                // Over everything behind the panel (under the panel itself): the tap
                                // goes nowhere else. A drag here does nothing either.
                                Color.clear
                                    .contentShape(Rectangle())
                                    .ignoresSafeArea()
                                    .onTapGesture { item = nil }
                                    .accessibilityHidden(true)
                            }
                            // Another item (a word sent while it is open) only changes what is in it.
                            BottomPanel(title: title, compact: compact, space: space.size, expanded: $expanded,
                                        onClose: { item = nil }) {
                                panel(current).id(current.id)
                            }
                        }
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                }
                .animation(PanelMotion.spring, value: item?.id)
            }
            .onChange(of: item?.id) { old, new in
                guard new == nil else { return }
                expanded = false
                if old != nil { onDismiss?() }
            }
    }
}

extension View {
    /// `item` in a BottomPanel over this view while it is not nil (instead of a sheet: compact
    /// at the bottom, what is behind stays visible; a tap outside the panel only closes it).
    /// `onDismiss` runs once it closed (its close button, a drag down, a tap outside, `item` set
    /// to nil), not when another item replaces it.
    /// The panel lives in this view: it must not use `dismiss`, a toolbar or a navigation title.
    func bottomPanel<Item: Identifiable, Panel: View>(item: Binding<Item?>, title: String = "", compact: CGFloat = 340,
                                                       onDismiss: (() -> Void)? = nil,
                                                       @ViewBuilder content: @escaping (Item) -> Panel) -> some View {
        modifier(BottomPanelPresenter(item: item, title: title, compact: compact, onDismiss: onDismiss, panel: content))
    }

    /// A BottomPanel shown while `isPresented` is true (closing it sets it to false). `keyboard`:
    /// it has a text field (see BottomPanelPresenter). Used by the grammar chat: a tap outside
    /// does not close it, what is behind stays usable.
    func bottomPanel<Panel: View>(isPresented: Binding<Bool>, title: String = "", compact: CGFloat = 340,
                                  keyboard: Bool = false, @ViewBuilder content: @escaping () -> Panel) -> some View {
        modifier(BottomPanelPresenter(item: Binding(get: { isPresented.wrappedValue ? PanelShown.shown : nil },
                                                    set: { isPresented.wrappedValue = $0 != nil }),
                                      title: title, compact: compact, onDismiss: nil, keyboard: keyboard,
                                      closesOnOutsideTap: false, panel: { _ in content() }))
    }
}
