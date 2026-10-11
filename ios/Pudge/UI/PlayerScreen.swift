import SwiftUI
import PudgeCore

struct MiniPlayer: View {
    @ObservedObject var player: AudioPlayerService
    var open: () -> Void
    var body: some View {
        if let book = player.book, player.embeddedControls == 0 {
            HStack {
                Button(action: open) {
                    VStack(alignment: .leading) { Text(book.title).lineLimit(1); Text(PudgeTheme.time(player.positionMs)).font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain)
                Button { if player.playing { player.pause() } else { player.resume() } } label: {
                    Image(systemName: player.playing ? "pause.fill" : "play.fill").frame(width: 48, height: 48)
                }.accessibilityLabel(player.playing ? "Pause" : "Play")
            }.padding(.horizontal, 16).background(PudgeTheme.panel)
        }
    }
}

struct PlayerScreen: View {
    @ObservedObject var model: PudgeModel
    @ObservedObject var player: AudioPlayerService
    @Environment(\.dismiss) private var dismiss
    @State private var dragging = false
    @State private var target = 0.0
    @State private var bookmarkTitle = ""
    @State private var bookmarkPrompt = false
    @State private var readingAlong = false
    @State private var reader: Int?
    var body: some View {
        NavigationStack {
            ScrollView {
                if let book = player.book {
                    VStack(spacing: 24) {
                        BookCover(store: model.store, book: book, width: 160)
                        Text(book.title).font(.title2.bold()).multilineTextAlignment(.center)
                        Text(book.chapters.last(where: { $0.startMs <= player.positionMs })?.title ?? "Audiobook").font(.subheadline).foregroundStyle(.secondary)
                        VStack {
                            Slider(value: Binding(get: { dragging ? target : Double(player.positionMs) }, set: { target = $0 }), in: 0...Double(book.durationMs)) { editing in
                                if editing { target = Double(player.positionMs); dragging = true }
                                else { dragging = false; Task { do { try await player.seek(Int64(target)) } catch { model.message = error.localizedDescription } } }
                            }.accessibilityLabel("Position in audiobook")
                            HStack { Text(PudgeTheme.time(Int64(dragging ? target : Double(player.positionMs)))); Spacer(); Text("−" + PudgeTheme.time(book.durationMs - player.positionMs)) }.font(.caption.monospacedDigit())
                        }
                        HStack(spacing: 28) {
                            Button { player.jump(-15) } label: { Image(systemName: "gobackward.15").font(.largeTitle).frame(minWidth: 48, minHeight: 48) }.accessibilityLabel("Back 15 seconds")
                            Button { if player.playing { player.pause() } else { player.resume() } } label: { Image(systemName: player.playing ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 70)) }.accessibilityLabel(player.playing ? "Pause" : "Play")
                            Button { player.jump(15) } label: { Image(systemName: "goforward.15").font(.largeTitle).frame(minWidth: 48, minHeight: 48) }.accessibilityLabel("Forward 15 seconds")
                        }
                        HStack {
                            Menu {
                                ForEach([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3], id: \.self) { speed in Button(String(format: "%g×", speed)) { player.setRate(Float(speed)) } }
                            } label: { Label(String(format: "%g×", player.rate), systemImage: "speedometer").frame(minHeight: 44) }
                            Spacer()
                            Button { bookmarkPrompt = true } label: { Label("Bookmark", systemImage: "bookmark").frame(minHeight: 44) }
                        }
                        Menu {
                            ForEach([5, 15, 30, 45, 60], id: \.self) { minutes in Button("\(minutes) min") { player.setSleep(minutes: minutes) } }
                            Button("End of chapter") { player.setSleep(minutes: nil, chapterEnd: true) }
                            Button("Turn off timer") { player.setSleep(minutes: nil) }
                        } label: { Label(player.sleepRemaining.map { "Timer: \($0 / 60):\(String(format: "%02d", $0 % 60))" } ?? "Sleep timer", systemImage: "moon").frame(minHeight: 44) }
                        if let error = player.message { Text(error).foregroundStyle(.red).font(.caption) }
                        if model.readAlong(book) != nil {
                            // The light-novel reader follows the audio word by word (as on the Mac).
                            // Without the Mac the reader needs the downloaded volume; otherwise the plain read-along text.
                            Button {
                                if let id = book.lightNovelId, !model.macOffline || model.media.isDownloaded("ln", id) { reader = id } else { readingAlong = true }
                            } label: {
                                Label("Read along", systemImage: "text.book.closed").frame(minHeight: 44)
                            }.buttonStyle(.borderedProminent)
                        }
                        Button("Finished") { player.pause(); player.save("finish") }
                        Button("Close player") { player.stop(); dismiss() }.foregroundStyle(.secondary)
                    }.padding(24).frame(maxWidth: 640).frame(maxWidth: .infinity)
                } else { ContentUnavailableView("Player closed", systemImage: "headphones") }
            }.background(PudgeTheme.background)
                .navigationTitle("Now playing").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .sheet(item: Binding(get: { reader.map(ReaderTarget.init) }, set: { reader = $0?.id })) { target in
                    NavigationStack {
                        NovelReaderScreen(media: model.media, bookId: target.id, title: player.book?.title ?? "Light novel")
                            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { reader = nil } } }
                    }.tint(PudgeTheme.accent).preferredColorScheme(.dark)
                }
                .sheet(isPresented: $readingAlong) {
                    if let book = player.book, let document = model.readAlong(book) {
                        ReadAlongScreen(player: player, media: model.media, document: document)
                    }
                }
                .alert("New bookmark", isPresented: $bookmarkPrompt) {
                    TextField("Title", text: $bookmarkTitle)
                    Button("Add") { if let book = player.book { model.addBookmark(book, title: bookmarkTitle); bookmarkTitle = "" } }
                    Button("Cancel", role: .cancel) { }
                }
        }.tint(PudgeTheme.accent).preferredColorScheme(.dark)
    }
}


/// Light-novel text following the audiobook: the spoken sentence is highlighted
/// and kept in view; tapping a paragraph jumps the audio there.
struct ReadAlongScreen: View {
    @ObservedObject var player: AudioPlayerService
    @ObservedObject var media: MediaModel
    let document: ReadAlongDocument
    @Environment(\.dismiss) private var dismiss
    @State private var lookup: WordLookup?
    @State private var resumeAfterLookup = false
    @State private var follow = true
    @State private var pickedChapter = 0
    @AppStorage("pudge.readAlongFontSize") private var fontSize = 19.0

    var body: some View {
        let live = document.location(atMs: player.positionMs)
        let chapterIndex = min(max(0, follow ? (live?.chapter ?? pickedChapter) : pickedChapter), document.chapters.count - 1)
        let chapter = document.chapters[chapterIndex]
        let current = live?.chapter == chapterIndex ? live?.sentence : nil
        let ranges = chapter.paragraphRanges
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 16) {
                        Text(chapter.title).font(.title3.bold()).padding(.bottom, 4)
                        ForEach(ranges, id: \.lowerBound) { range in
                            if let current, range.contains(current) {
                                // Words of the spoken sentence are tappable (definition, Add/Review).
                                paragraph(chapter, range: range, current: current)
                                    .id(range.lowerBound)
                            } else {
                                paragraph(chapter, range: range, current: current)
                                    .id(range.lowerBound)
                                    .contentShape(Rectangle())
                                    .onTapGesture {
                                        follow = true
                                        let target = chapter.timeMs(atOffset: chapter.sentences[range.lowerBound].start)
                                        Task { try? await player.seek(target) }
                                    }
                            }
                        }
                    }.padding(20).frame(maxWidth: PudgeTheme.readingWidth).frame(maxWidth: .infinity)
                }
                .onChange(of: current) { _, value in
                    guard follow, let value, let range = ranges.first(where: { $0.contains(value) }) else { return }
                    withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(range.lowerBound, anchor: .center) }
                }
                .onAppear {
                    pickedChapter = live?.chapter ?? 0
                    if let value = current, let range = ranges.first(where: { $0.contains(value) }) { proxy.scrollTo(range.lowerBound, anchor: .center) }
                }
            }
            .background(PudgeTheme.background)
            .environment(\.openURL, OpenURLAction { url in
                guard let hit = StudyStyle.parse(url) else { return .systemAction }
            guard let sentence = chapter.sentences[safe: hit.0],
                      let word = sentence.words[safe: hit.1] else { return .discarded }
                let surface = StudyText.segments(sentence.text, tokens: sentence.words.map(\.token)).first { $0.token == hit.1 }?.text ?? ""
                resumeAfterLookup = player.playing
                player.pause()
                let target = chapter.timeMs(atOffset: word.offset)
                lookup = WordLookup(token: word.token, surface: surface, sentence: sentence.text, playFromHere: {
                    lookup = nil; follow = true
                    Task { try? await player.seek(target); player.resume() }
                })
                return .handled
            })
            .sheet(item: $lookup, onDismiss: {
                if resumeAfterLookup { resumeAfterLookup = false; if !player.playing { player.resume() } }
            }) { item in WordSheet(media: media, lookup: item) }
            .navigationTitle(document.title.isEmpty ? "Read along" : document.title).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Button(follow ? "Following the audio" : "Follow the audio") { follow = true }
                        Section("Text size") {
                            Button("Larger") { fontSize = min(30, fontSize + 2) }
                            Button("Smaller") { fontSize = max(13, fontSize - 2) }
                        }
                        Section("Chapters") {
                            ForEach(Array(document.chapters.enumerated()), id: \.offset) { index, item in
                                Button(item.title) { follow = false; pickedChapter = index }
                            }
                        }
                    } label: { Image(systemName: "ellipsis.circle") }
                }
            }
            .safeAreaInset(edge: .bottom) {
                HStack(spacing: 32) {
                    Button { player.jump(-15) } label: { Image(systemName: "gobackward.15").font(.title2) }.accessibilityLabel("Back 15 seconds")
                    Button { if player.playing { player.pause() } else { player.resume() } } label: {
                        Image(systemName: player.playing ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 44))
                    }.accessibilityLabel(player.playing ? "Pause" : "Play")
                    Button { player.jump(15) } label: { Image(systemName: "goforward.15").font(.title2) }.accessibilityLabel("Forward 15 seconds")
                    if !follow { Button("Follow") { follow = true }.font(.callout) }
                }.frame(maxWidth: .infinity).padding(.vertical, 10).background(PudgeTheme.panel)
            }
        }.tint(PudgeTheme.accent).preferredColorScheme(.dark)
    }

    private func paragraph(_ chapter: ReadAlongChapter, range: Range<Int>, current: Int?) -> some View {
        var result = AttributedString()
        for index in range {
            let sentence = chapter.sentences[index]
            let color: Color = index == current ? PudgeTheme.accent : (current.map { index < $0 } ?? false) ? .primary : .secondary
            var piece: AttributedString
            if index == current, !sentence.words.isEmpty {
                // The word being spoken is highlighted; links take the accent tint.
                let spoken = sentence.word(atOffset: chapter.offset(atMs: player.positionMs))
                piece = studyText(sentence.text, tokens: sentence.words.map(\.token), group: index, highlight: spoken, base: color, marks: false)
            } else {
                piece = AttributedString(sentence.text)
                piece[AttributeScopes.SwiftUIAttributes.ForegroundColorAttribute.self] = color
            }
            if index == current {
                piece[AttributeScopes.FoundationAttributes.InlinePresentationIntentAttribute.self] = .stronglyEmphasized
            }
            result += piece
        }
        return Text(result).font(.system(size: fontSize)).lineSpacing(6).tint(PudgeTheme.accent)
    }
}

struct ReaderTarget: Identifiable { let id: Int }
