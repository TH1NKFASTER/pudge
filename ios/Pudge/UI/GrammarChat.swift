import Foundation
import SwiftUI
import UIKit
import PudgeCore

/// "Explain grammar" in a reader (light novel, manga): a chat with the Mac's LLM about one
/// sentence. The Mac explains it as its own reader does (pudge/iphone_grammar.py), then answers
/// questions about it. Chats are kept on this device only, the last few per book
/// (GrammarChatArchive). The panel hides and comes back: while a chat exists a small button in
/// the reader shows it again.
@MainActor
final class GrammarChatModel: ObservableObject {
    @Published private(set) var chat: GrammarChat?
    @Published var shown = false
    @Published private(set) var pending = false
    @Published private(set) var failure: String?
    /// The book's chats, the one used last first.
    @Published private(set) var recent: [GrammarChat] = []
    private var book = ""
    private var work: Task<Void, Never>?

    private static let store = GrammarChatStore(file: GrammarChatModel.file)
    private static var file: URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("PudgeAudio/grammar-chats.json")
    }

    /// The reader opened a book: its last chat comes back behind the button (not shown).
    func open(book key: String) {
        guard key != book else { return }
        stop()
        book = key
        failure = nil
        shown = false
        recent = Self.store.archive.chats(book: key)
        chat = recent.first
    }

    /// The reader closed: a question on its way is dropped (Try again sends it later).
    func stop() {
        work?.cancel()
        work = nil
        pending = false
    }

    /// A new chat about `source`, shown at once; the Mac explains it. `context`: the text around
    /// it, gathered first (a manga page's text boxes; skipped while the Mac is away).
    func explain(_ source: GrammarSource, media: MediaModel, context more: (@MainActor () async -> String)? = nil) {
        let sentence = source.sentence.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !sentence.isEmpty else { return }
        stop()
        var next = GrammarChat(book: book, source: GrammarSource(sentence: sentence, context: source.context))
        next.append(GrammarChatMessage(role: .user, text: GrammarChat.request(sentence)))
        chat = next
        failure = nil
        shown = true
        save()
        guard sentence.unicodeScalars.count <= GrammarLimits.sentence else {
            failure = "The selection is too long (\(GrammarLimits.sentence) characters at most): select one or two sentences."
            return
        }
        guard let more, !media.app.macOffline else { send(media); return }
        pending = true
        let id = next.id
        work = Task { [weak self] in
            let context = await more()
            guard !Task.isCancelled, let self, var current = self.chat, current.id == id else { return }
            current.source.context = context
            self.chat = current
            self.save()
            self.work = nil
            self.send(media)
        }
    }

    /// A question about the sentence (after its explanation); false when it was not sent.
    func ask(_ question: String, media: MediaModel) -> Bool {
        let text = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !pending, var current = chat, current.explained else { return false }
        guard text.unicodeScalars.count <= GrammarLimits.question else {
            failure = "The question is too long (\(GrammarLimits.question) characters at most)."
            return false
        }
        current.append(GrammarChatMessage(role: .user, text: text))
        chat = current
        save()
        send(media)
        return true
    }

    /// The last question (or the explanation) again.
    func retry(media: MediaModel) {
        guard chat?.waitingForAnswer == true, !pending else { return }
        send(media)
    }

    /// Another of the book's recent chats.
    func select(_ id: UUID) {
        guard let found = recent.first(where: { $0.id == id }), found.id != chat?.id else { return }
        stop()
        failure = nil
        chat = found
    }

    func deleteCurrent() {
        guard let current = chat else { return }
        stop()
        failure = nil
        Self.store.remove(current.id)
        recent = Self.store.archive.chats(book: book)
        chat = recent.first
        if chat == nil { shown = false }
    }

    private func save() {
        guard let chat else { return }
        Self.store.save(chat)
        recent = Self.store.archive.chats(book: book)
    }

    private func send(_ media: MediaModel) {
        guard let current = chat else { return }
        work?.cancel()
        failure = nil
        if media.app.macOffline {
            pending = false
            failure = "Explain grammar needs the Mac: the \(PudgeTheme.device) cannot reach it now. Try again when it can."
            return
        }
        pending = true
        let id = current.id
        work = Task { [weak self] in
            do {
                let client = try media.connection()
                let answer: GrammarJob
                if current.explained {
                    let request = current.askRequest
                    answer = try await GrammarChatModel.answer(client) { try await client.media("grammar/ask", post: request, as: GrammarJob.self) }
                } else {
                    let request = GrammarExplainRequest(current.source)
                    answer = try await GrammarChatModel.answer(client) { try await client.media("grammar/explain", post: request, as: GrammarJob.self) }
                }
                guard !Task.isCancelled else { return }
                self?.received(answer, chat: id)
            } catch {
                guard !Task.isCancelled else { return }
                self?.failed(error.localizedDescription, chat: id)
            }
        }
    }

    /// Starts the question and asks for its answer until it is there (each request waits up to
    /// a few seconds on the Mac; a slow LLM takes a minute or more).
    private static func answer(_ client: MacConnection, start: () async throws -> GrammarJob) async throws -> GrammarJob {
        var answer = try await start()
        let deadline = Date().addingTimeInterval(360)
        while answer.running, let job = answer.job {
            try Task.checkCancellation()
            guard Date() < deadline else { throw PudgeError.invalid("The Mac's LLM did not answer within 6 minutes. Try again.") }
            answer = try await client.media("grammar/jobs/\(job)?wait=1", as: GrammarJob.self)
        }
        return answer
    }

    private func received(_ answer: GrammarJob, chat id: UUID) {
        guard var current = chat, current.id == id else { return }
        pending = false
        work = nil
        guard answer.state == "done" else {
            failure = [answer.error ?? "The Mac did not answer.", answer.detail ?? ""].filter { !$0.isEmpty }.joined(separator: "\n")
            return
        }
        if let explanation = answer.explanation {
            current.append(GrammarChatMessage(role: .assistant, text: explanation.translation, explanation: explanation))
        } else {
            current.append(GrammarChatMessage(role: .assistant, text: answer.reply ?? ""))
        }
        chat = current
        failure = nil
        save()
    }

    private func failed(_ reason: String, chat id: UUID) {
        guard chat?.id == id else { return }
        pending = false
        work = nil
        failure = reason
    }
}

/// The chat panel's content: the explanation first (translation and grammar points), then the
/// questions and answers, a field for the next question at the bottom. An answer's Markdown is
/// drawn (ChatMarkdownView); never HTML, and only web links open (in the browser).
struct GrammarChatPanel: View {
    @ObservedObject var chats: GrammarChatModel
    @ObservedObject var media: MediaModel
    @State private var question = ""
    @FocusState private var typing: Bool

    var body: some View {
        VStack(spacing: 0) {
            if chats.chat != nil {
                chatBar
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if let chat = chats.chat {
                            ForEach(chat.messages) { message in
                                GrammarMessageView(message: message).id(message.id)
                            }
                        }
                        status.id("status")
                    }
                    .padding(.horizontal, 16).padding(.vertical, 8)
                }
                .scrollDismissesKeyboard(.interactively)
                .onAppear { proxy.scrollTo("status", anchor: .bottom) }
                .onChange(of: chats.chat?.messages.count) { _, _ in
                    withAnimation { proxy.scrollTo("status", anchor: .bottom) }
                }
                .onChange(of: chats.pending) { _, _ in
                    withAnimation { proxy.scrollTo("status", anchor: .bottom) }
                }
            }
            Divider()
            input
        }
    }

    /// The sentence, the book's other recent chats and Delete.
    private var chatBar: some View {
        HStack {
            Text(chats.chat?.source.sentence ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 8)
            Menu {
                if chats.recent.count > 1 {
                    Section("Recent chats in this book") {
                        ForEach(chats.recent) { item in
                            Button {
                                chats.select(item.id)
                            } label: {
                                if item.id == chats.chat?.id {
                                    Label(String(item.source.sentence.prefix(40)), systemImage: "checkmark")
                                } else {
                                    Text(String(item.source.sentence.prefix(40)))
                                }
                            }
                        }
                    }
                }
                if chats.chat != nil {
                    Button(role: .destructive) { chats.deleteCurrent() } label: { Label("Delete this chat", systemImage: "trash") }
                }
            } label: {
                Image(systemName: "clock.arrow.circlepath").frame(width: 36, height: 28)
            }
            .accessibilityLabel("Chats in this book")
        }
        .padding(.horizontal, 16).padding(.bottom, 4)
    }

    @ViewBuilder private var status: some View {
        if chats.pending {
            HStack(spacing: 8) {
                ProgressView()
                Text(chats.chat?.explained == true ? "The Mac is asking its LLM…" : "Explaining…").font(.callout).foregroundStyle(.secondary)
            }
        } else if let failure = chats.failure {
            VStack(alignment: .leading, spacing: 6) {
                Label(failure, systemImage: "exclamationmark.triangle").font(.callout).foregroundStyle(.orange)
                    .textSelection(.enabled)
                if chats.chat?.waitingForAnswer == true {
                    Button("Try again") { chats.retry(media: media) }.buttonStyle(.bordered)
                }
            }
        } else if chats.chat?.waitingForAnswer == true {
            // Not answered before the reader closed.
            Button("Try again") { chats.retry(media: media) }.buttonStyle(.bordered)
        } else {
            Color.clear.frame(height: 1)
        }
    }

    private var canSend: Bool {
        chats.chat?.explained == true && !chats.pending && !question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var input: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField(chats.chat?.explained == true ? "Ask about this sentence…" : "Questions after the explanation",
                      text: $question, axis: .vertical)
                .lineLimit(1...4)
                .textFieldStyle(.roundedBorder)
                .focused($typing)
                .disabled(chats.chat?.explained != true)
            Button { send() } label: { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                .disabled(!canSend)
                .accessibilityLabel("Send")
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    private func send() {
        if chats.ask(question, media: media) { question = "" }
    }
}

private struct GrammarMessageView: View {
    let message: GrammarChatMessage
    var body: some View {
        if message.role == .user {
            HStack {
                Spacer(minLength: 40)
                Text(message.text).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(PudgeTheme.accent.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
                    .textSelection(.enabled)
            }
        } else if let explanation = message.explanation {
            GrammarExplanationView(explanation: explanation)
        } else {
            ChatMarkdownView(text: message.text)
        }
    }
}

/// An LLM answer drawn from its Markdown (ChatMarkdown): paragraphs, headings, lists, quotes and
/// fenced code (monospaced, scrolls sideways, Copy); inline bold, italics, `code` and web links.
/// A partial answer (an open fence) shows its code so far.
struct ChatMarkdownView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(ChatMarkdown.blocks(text).enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func blockView(_ block: ChatBlock) -> some View {
        switch block {
        case .paragraph(let value):
            Text(Self.styled(value)).textSelection(.enabled)
        case .heading(let level, let value):
            Text(Self.styled(value)).font(level <= 1 ? .title3.bold() : level == 2 ? .headline : .subheadline.bold())
                .textSelection(.enabled).padding(.top, 2)
        case .code(let language, let code, _):
            ChatCodeBlock(language: language, code: code)
        case .list(_, let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(item.marker).monospacedDigit().foregroundStyle(.secondary)
                        Text(Self.styled(item.text)).textSelection(.enabled)
                    }
                    .padding(.leading, CGFloat(item.level) * 16)
                }
            }
        case .quote(let value):
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 1.5).fill(Color.secondary.opacity(0.5)).frame(width: 3)
                Text(Self.styled(value)).foregroundStyle(.secondary).textSelection(.enabled)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .rule:
            Divider()
        }
    }

    /// Inline Markdown with `code` monospaced on a subtle background and links in the accent colour.
    static func styled(_ value: String) -> AttributedString {
        var shown = ChatMarkdown.inline(value)
        let codes = shown.runs.filter { $0.inlinePresentationIntent?.contains(.code) == true }.map(\.range)
        let links = shown.runs.filter { $0.link != nil }.map(\.range)
        for range in codes {
            shown[range].swiftUI.font = .system(.body, design: .monospaced)
            shown[range].swiftUI.backgroundColor = Color.white.opacity(0.1)
        }
        for range in links {
            shown[range].swiftUI.foregroundColor = PudgeTheme.accent
            shown[range].swiftUI.underlineStyle = .single
        }
        return shown
    }
}

/// A fenced code block: its language, Copy, the code monospaced in one line per line (scrolls
/// sideways rather than wrapping).
private struct ChatCodeBlock: View {
    let language: String
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language).font(.caption.monospaced()).foregroundStyle(.secondary)
                Spacer()
                Button {
                    UIPasteboard.general.string = code
                    copied = true
                    Task { try? await Task.sleep(nanoseconds: 1_500_000_000); copied = false }
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc").font(.caption)
                }
                .buttonStyle(.borderless)
                .accessibilityLabel("Copy the code")
            }
            .padding(.horizontal, 10).padding(.vertical, 6)
            ScrollView(.horizontal, showsIndicators: true) {
                Text(code.isEmpty ? " " : code)
                    .font(.system(.callout, design: .monospaced))
                    .fixedSize(horizontal: true, vertical: false)
                    .textSelection(.enabled)
                    .padding(.horizontal, 10).padding(.bottom, 10)
            }
        }
        .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.08)))
    }
}

/// The Mac's explanation: the translation, then each grammar point (its pattern, the meaning
/// here, a short explanation, the form).
private struct GrammarExplanationView: View {
    let explanation: GrammarExplanation

    /// "～が好き — likes": the pattern in bold, then its meaning here (plain text from the Mac).
    private static func headline(_ point: GrammarPoint) -> AttributedString {
        var line = AttributedString(point.pattern)
        line[AttributeScopes.FoundationAttributes.InlinePresentationIntentAttribute.self] = .stronglyEmphasized
        if !point.meaning.isEmpty { line += AttributedString(" — " + point.meaning) }
        return line
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !explanation.translation.isEmpty {
                Text(explanation.translation).italic()
            }
            if explanation.points.isEmpty {
                Text("No notable grammar points.").foregroundStyle(.secondary)
            }
            ForEach(Array(explanation.points.enumerated()), id: \.offset) { _, point in
                VStack(alignment: .leading, spacing: 3) {
                    Text(Self.headline(point))
                    if !point.explanation.isEmpty { Text(point.explanation).font(.callout) }
                    if !point.form.isEmpty { Text(point.form).font(.caption).foregroundStyle(.secondary) }
                }
            }
            Text("Explained by the LLM set up on the Mac; it may contain mistakes.").font(.caption2).foregroundStyle(.secondary)
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
    }
}

/// While the chat is hidden, it comes back with this button (in the reader's corner).
struct GrammarChatButton: View {
    @ObservedObject var chats: GrammarChatModel
    /// Before the chat shows (the reader closes its word card, which would cover the chat).
    var onShow: () -> Void = {}
    var body: some View {
        if chats.chat != nil && !chats.shown {
            Button { onShow(); withAnimation(PanelMotion.spring) { chats.shown = true } } label: {
                Image(systemName: chats.pending ? "ellipsis.bubble" : "text.bubble").font(.title3)
                    .foregroundStyle(PudgeTheme.accent).frame(width: 44, height: 44)
                    .background(PudgeTheme.panel.opacity(0.92), in: Circle())
            }
            .accessibilityLabel("Show the grammar chat")
            .padding(.leading, 16).padding(.bottom, 12)
        }
    }
}
