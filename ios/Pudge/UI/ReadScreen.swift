import SwiftUI
import Combine
import PudgeCore

struct NovelRoute: Hashable { let id: Int; let title: String }
struct ShelfRoute: Hashable { let key: String }
struct MangaRoute: Hashable { let id: Int }

extension LightNovelItem {
    var seriesInfo: (key: String, title: String) { (seriesKey ?? "book:\(id)", seriesTitle ?? title) }
    var franchiseInfo: (key: String, title: String, order: Int)? {
        guard let key = franchiseKey, !key.isEmpty else { return nil }
        return (key, franchiseTitle?.isEmpty == false ? franchiseTitle! : (seriesTitle ?? title), franchiseOrder ?? 0)
    }
    var started: Bool { (progress ?? 0) > 0 || chapter > 0 || offset > 0 }
}

/// The shelf's volume as the Mac library picks it (cover and Continue): the earliest
/// unfinished volume, series in order; when everything is finished, the last volume.
func macCurrent<Item>(_ shelf: Shelf<Item>, finished: (Item) -> Bool) -> Item? {
    for series in shelf.series {
        if let item = series.items.first(where: { !finished($0) }) { return item }
    }
    return shelf.series.last?.items.last
}

/// Light novels from the Mac, grouped by franchise → series → volumes like the Mac library.
struct NovelsScreen: View {
    @ObservedObject var media: MediaModel
    @State private var search = ""
    @State private var path = NavigationPath()
    var shelves: [Shelf<LightNovelItem>] {
        let items = media.novels.filter { search.isEmpty || $0.title.localizedCaseInsensitiveContains(search)
            || ($0.franchiseTitle ?? "").localizedCaseInsensitiveContains(search) }
        return Shelves.group(items, franchise: { $0.franchiseInfo }, series: { $0.seriesInfo },
                             volume: { $0.volume ?? 0 }, updated: { $0.updatedAt })
    }
    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let error = media.readingError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
                ReadingNote(media: media)
                if shelves.isEmpty && !media.loadingReading {
                    ContentUnavailableView("No light novels", systemImage: "book", description: Text("Light novels from the Mac's library appear here. Download a volume to read it without the Mac."))
                }
                ForEach(shelves) { shelf in
                    let items = shelf.items
                    let target = macCurrent(shelf, finished: { $0.finished })
                    Section {
                        if items.count == 1, let book = items.first {
                            NavigationLink(value: NovelRoute(id: book.id, title: book.title)) { NovelRow(media: media, book: book, showTitle: true) }
                        } else {
                            NavigationLink(value: ShelfRoute(key: shelf.key)) {
                                HStack(spacing: 14) {
                                    RemoteCover(media: media, path: "cover/ln/\((target ?? items[0]).id)", width: 58, symbol: "book")
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(shelf.title).font(.headline).lineLimit(2)
                                        Text("\(items.count) volumes · \(items.filter(\.finished).count) finished").font(.caption).foregroundStyle(.secondary)
                                        if items.contains(where: \.hasAudio) { Label("Audiobook linked", systemImage: "headphones").font(.caption2).foregroundStyle(PudgeTheme.accent) }
                                    }
                                }
                            }
                            if let target, !target.finished {
                                PillButton(title: "Continue " + (target.volume.map { "vol. \($0)" } ?? target.title), systemImage: "book.fill") {
                                    path.append(NovelRoute(id: target.id, title: target.title))
                                }
                            }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden).background(PudgeTheme.background)
            .navigationTitle("Light Novels").searchable(text: $search, prompt: "Title")
            .navigationDestination(for: NovelRoute.self) { route in NovelReaderScreen(media: media, bookId: route.id, title: route.title) }
            .navigationDestination(for: ShelfRoute.self) { route in
                if let shelf = shelves.first(where: { $0.key == route.key }) { NovelShelfScreen(media: media, shelf: shelf) }
            }
            .toolbar { ToolbarItem(placement: .primaryAction) { SettingsButton(model: media.app) } }
            .overlay { if media.loadingReading && media.novels.isEmpty { ProgressView() } }
            .refreshable { await media.refreshReading(forceUpdates: true) }
            .task { if media.novels.isEmpty { await media.refreshReading() } }
        }
    }
}

struct NovelRow: View {
    @ObservedObject var media: MediaModel
    let book: LightNovelItem
    var showTitle = false
    var body: some View {
        HStack(spacing: 14) {
            RemoteCover(media: media, path: "cover/ln/\(book.id)", symbol: "book")
            VStack(alignment: .leading, spacing: 4) {
                Text(showTitle || book.volume == nil ? book.title : "Volume \(book.volume!)").font(.headline).lineLimit(2)
                Text(book.finished ? "Finished" : "Chapter \(min(book.chapters, book.chapter + 1)) of \(book.chapters)")
                    .font(.caption).foregroundStyle(.secondary)
                if let progress = book.progress, progress > 0, !book.finished { ProgressView(value: progress) }
                if book.hasAudio { Label("Audiobook linked", systemImage: "headphones").font(.caption2).foregroundStyle(PudgeTheme.accent) }
                ReadingDownloadStatus(media: media, kind: "ln", id: book.id)
            }
        }
        .contextMenu { ReadingDownloadMenu(media: media, kind: "ln", id: book.id, title: book.title) }
    }
}

/// Volumes of one franchise or series.
struct NovelShelfScreen: View {
    @ObservedObject var media: MediaModel
    let shelf: Shelf<LightNovelItem>
    var body: some View {
        List {
            ForEach(shelf.series) { series in
                Section(shelf.series.count > 1 ? series.title : "") {
                    ForEach(series.items) { book in
                        NavigationLink(value: NovelRoute(id: book.id, title: book.title)) { NovelRow(media: media, book: book) }
                    }
                }
            }
        }
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        .navigationTitle(shelf.title).navigationBarTitleDisplayMode(.inline)
    }
}

/// Manga grouped by series → volumes like the Mac library.
struct MangaScreen: View {
    @ObservedObject var media: MediaModel
    @State private var search = ""
    @State private var path = NavigationPath()
    var shelves: [Shelf<MangaItem>] {
        let items = media.manga.filter { search.isEmpty || $0.title.localizedCaseInsensitiveContains(search) }
        return Shelves.group(items, franchise: { _ in nil }, series: { ($0.seriesKey ?? "book:\($0.id)", $0.seriesTitle ?? $0.title) },
                             volume: { $0.volume ?? 0 }, updated: { $0.updatedAt })
    }
    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let error = media.readingError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
                ReadingNote(media: media)
                if shelves.isEmpty && !media.loadingReading {
                    ContentUnavailableView("No manga", systemImage: "book.pages", description: Text("Manga from the Mac's library appear here. Download a volume to read it without the Mac."))
                }
                ForEach(shelves) { shelf in
                    let items = shelf.items
                    let target = macCurrent(shelf, finished: { $0.pages > 0 && $0.readPages >= $0.pages })
                    Section {
                        if items.count == 1, let book = items.first {
                            NavigationLink(value: MangaRoute(id: book.id)) { MangaRow(media: media, book: book, showTitle: true) }
                        } else {
                            NavigationLink(value: ShelfRoute(key: shelf.key)) {
                                HStack(spacing: 14) {
                                    RemoteCover(media: media, path: "cover/manga/\((target ?? items[0]).id)", width: 58, symbol: "book.pages")
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(shelf.title).font(.headline).lineLimit(2)
                                        Text("\(items.count) volumes · \(items.filter { $0.pages > 0 && $0.readPages >= $0.pages }.count) read")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                            if let target, !(target.pages > 0 && target.readPages >= target.pages) {
                                PillButton(title: "Continue vol. \(target.volume ?? 1)", systemImage: "book.pages.fill") {
                                    path.append(MangaRoute(id: target.id))
                                }
                            }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden).background(PudgeTheme.background)
            .navigationTitle("Manga").searchable(text: $search, prompt: "Title")
            .navigationDestination(for: MangaRoute.self) { route in
                if let book = media.manga.first(where: { $0.id == route.id }) { MangaReaderScreen(media: media, book: book) }
            }
            .navigationDestination(for: ShelfRoute.self) { route in
                if let shelf = shelves.first(where: { $0.key == route.key }) { MangaShelfScreen(media: media, shelf: shelf) }
            }
            .toolbar { ToolbarItem(placement: .primaryAction) { SettingsButton(model: media.app) } }
            .overlay { if media.loadingReading && media.manga.isEmpty { ProgressView() } }
            .refreshable { await media.refreshReading(forceUpdates: true) }
            .task { if media.manga.isEmpty { await media.refreshReading() } }
        }
    }
}

struct MangaRow: View {
    @ObservedObject var media: MediaModel
    let book: MangaItem
    var showTitle = false
    var body: some View {
        HStack(spacing: 14) {
            RemoteCover(media: media, path: "cover/manga/\(book.id)", symbol: "book.pages")
            VStack(alignment: .leading, spacing: 4) {
                Text(showTitle || book.volume == nil ? book.title : "Volume \(book.volume!)").font(.headline).lineLimit(2)
                Text("Page \(min(book.pages, book.position + 1)) of \(book.pages)").font(.caption).foregroundStyle(.secondary)
                ProgressView(value: Double(book.readPages), total: Double(max(1, book.pages)))
                if book.ocrPages > 0 { Label("Text on \(book.ocrPages) pages", systemImage: "text.viewfinder").font(.caption2).foregroundStyle(.secondary) }
                ReadingDownloadStatus(media: media, kind: "manga", id: book.id)
            }
        }
        .contextMenu {
            ReadingDownloadMenu(media: media, kind: "manga", id: book.id, title: book.title)
            if media.needsOCR(book) { GetOCRMenuItem(media: media, book: book) }
        }
    }
}

// MARK: Downloads for reading without the Mac

/// "On <this device>" or the download's progress under a volume in the lists.
struct ReadingDownloadStatus: View {
    @ObservedObject var media: MediaModel
    let kind: String
    let id: Int
    var body: some View {
        if let job = media.readingJobs["\(kind):\(id)"] {
            Label(job.phase + " \(Int(job.fraction * 100))%", systemImage: "arrow.down.circle").font(.caption2).foregroundStyle(PudgeTheme.accent)
        } else if let record = media.readingDownloads["\(kind):\(id)"], let deleted = record.deletedOnMacAt {
            // Deleted on the Mac: still readable here; removed a week later unless opened since.
            VStack(alignment: .leading, spacing: 1) {
                Label("Deleted on the Mac", systemImage: "trash.slash").font(.caption2.bold()).foregroundStyle(.orange)
                if (record.openedAt ?? 0) >= deleted {
                    Text("Kept on this \(PudgeTheme.device) until you remove it").font(.caption2).foregroundStyle(.secondary)
                } else {
                    Text("Removed from this \(PudgeTheme.device) on \(Date(timeIntervalSince1970: deleted + ReadingRevisions.keepDeleted).formatted(date: .abbreviated, time: .omitted)) unless you open it")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            .accessibilityIdentifier("deleted-on-mac")
        } else if media.isDownloaded(kind, id) {
            Label("On \(PudgeTheme.device)", systemImage: "checkmark.circle.fill").font(.caption2).foregroundStyle(PudgeTheme.good)
        }
    }
}

/// A short note after downloaded volumes were updated (or OCR arrived); dismissed with ×.
struct ReadingNote: View {
    @ObservedObject var media: MediaModel
    var body: some View {
        if let note = media.readingNote {
            HStack {
                Label(note, systemImage: "arrow.triangle.2.circlepath").font(.subheadline).foregroundStyle(PudgeTheme.good)
                Spacer()
                Button { media.readingNote = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless).accessibilityLabel("Dismiss")
            }
            .accessibilityIdentifier("reading-note")
        }
    }
}

/// "Get OCR" in a volume's long-press menu.
struct GetOCRMenuItem: View {
    @ObservedObject var media: MediaModel
    let book: MangaItem
    var body: some View {
        if media.ocrJobs[book.id] != nil {
            Button("Stop waiting for OCR", systemImage: "xmark.circle") { media.cancelOCR(book.id) }
        } else {
            Button("Get OCR", systemImage: "text.viewfinder") { Task { await media.getOCR(book.id, title: book.title) } }
        }
    }
}

/// "Get OCR" in the manga reader: the Mac OCRs the whole volume; the phone waits and then
/// takes the finished text boxes (shown only while the volume has no complete OCR).
struct GetOCRButton: View {
    @ObservedObject var media: MediaModel
    let book: MangaItem
    var body: some View {
        if let job = media.ocrJobs[book.id] {
            Menu {
                Text(job.phase)
                Button("Stop waiting", role: .destructive) { media.cancelOCR(book.id) }
            } label: { ProgressView() }
                .accessibilityLabel("Waiting for OCR")
        } else if media.needsOCR(book) {
            Button { Task { await media.getOCR(book.id, title: book.title) } } label: { Label("Get OCR", systemImage: "text.viewfinder") }
                .accessibilityIdentifier("get-ocr")
        }
    }
}

/// Long-press menu of a volume: download, cancel or remove.
struct ReadingDownloadMenu: View {
    @ObservedObject var media: MediaModel
    let kind: String
    let id: Int
    let title: String
    var body: some View {
        if media.readingJobs["\(kind):\(id)"] != nil {
            Button("Cancel download", systemImage: "xmark.circle", role: .destructive) { media.cancelReading(kind, id) }
        } else if media.isDownloaded(kind, id) {
            Button("Remove from \(PudgeTheme.device)", systemImage: "trash", role: .destructive) { media.deleteReading(kind, id) }
        } else {
            Button("Download to \(PudgeTheme.device)", systemImage: "arrow.down.circle") { Task { await media.downloadReading(kind, id: id, title: title) } }
                .disabled(media.app.macConnection == nil || media.app.macOffline)
        }
    }
}

/// The reader's download button: download, progress (tap to cancel) or on iPhone (tap to remove).
struct ReadingDownloadButton: View {
    @ObservedObject var media: MediaModel
    let kind: String
    let id: Int
    let title: String
    @State private var removePrompt = false
    var body: some View {
        Group {
            if let job = media.readingJobs["\(kind):\(id)"] {
                Menu {
                    Text(job.phase + " \(Int(job.fraction * 100))%")
                    Button("Cancel download", role: .destructive) { media.cancelReading(kind, id) }
                } label: { ProgressView() }
                    .accessibilityLabel("Downloading to \(PudgeTheme.device)")
            } else if media.isDownloaded(kind, id) {
                Button { removePrompt = true } label: { Image(systemName: "checkmark.circle.fill") }
                    .accessibilityLabel("On \(PudgeTheme.device). Remove download")
            } else {
                Button { Task { await media.downloadReading(kind, id: id, title: title) } } label: { Image(systemName: "arrow.down.circle") }
                    .accessibilityLabel("Download to \(PudgeTheme.device)")
                    .disabled(media.app.macConnection == nil || media.app.macOffline)
            }
        }
        .accessibilityIdentifier("reading-download")
        .confirmationDialog("Remove “\(title)” from the \(PudgeTheme.device)?", isPresented: $removePrompt, titleVisibility: .visible) {
            Button("Remove download", role: .destructive) { media.deleteReading(kind, id) }
        } message: { Text("It stays on the Mac. Reading it again needs the Mac or a new download.") }
    }
}

struct MangaShelfScreen: View {
    @ObservedObject var media: MediaModel
    let shelf: Shelf<MangaItem>
    var body: some View {
        List {
            ForEach(shelf.items) { book in
                NavigationLink(value: MangaRoute(id: book.id)) { MangaRow(media: media, book: book) }
            }
        }
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        .navigationTitle(shelf.title).navigationBarTitleDisplayMode(.inline)
    }
}

/// Settings → Storage: the light novels and manga volumes on this iPhone.
struct ReadingDownloadsScreen: View {
    @ObservedObject var media: MediaModel
    var body: some View {
        List {
            ForEach(media.readingDownloads.values.sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }, id: \.key) { record in
                HStack {
                    Label(record.title, systemImage: record.kind == "ln" ? "book" : "book.pages")
                    Spacer()
                    Text(PudgeTheme.size(record.sizeBytes)).font(.caption).foregroundStyle(.secondary)
                }
                .swipeActions { Button("Remove", role: .destructive) { media.deleteReading(record.kind, record.id) } }
            }
            Section { Text("Swipe to remove a volume. It stays on the Mac.").font(.caption).foregroundStyle(.secondary) }
        }
        .navigationTitle("Light novels and manga").navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: Light novel reader

/// Follows the linked audiobook in the reader: the spoken chapter, paragraph and word.
@MainActor
final class ReaderAudioFollower: ObservableObject {
    struct Spoken: Equatable { let chapter: Int; let paragraph: Int; let token: Int? }
    @Published private(set) var spoken: Spoken?
    /// Light-novel chapter the audio is in.
    @Published private(set) var audioChapter: Int?
    /// The linked audiobook is the one in the player.
    @Published private(set) var active = false
    /// The linked audiobook is playing (the reader scrolls along with it).
    @Published private(set) var playing = false
    private var cancellable: AnyCancellable?
    private var playingCancellable: AnyCancellable?
    private var document: ReadAlongDocument?
    private var assetId: String?
    private weak var player: AudioPlayerService?
    private var offsets: [Int: [Int]] = [:]
    var chapter: ChapterText? { didSet { offsets = [:]; update() } }

    func attach(player: AudioPlayerService, assetId: String?, document: ReadAlongDocument?) {
        self.player = player; self.assetId = assetId; self.document = document
        cancellable = player.$positionMs.receive(on: DispatchQueue.main).sink { [weak self] _ in
            MainActor.assumeIsolated { self?.update() }
        }
        playingCancellable = player.$playing.receive(on: DispatchQueue.main).sink { [weak self] _ in
            MainActor.assumeIsolated { self?.update() }
        }
        update()
    }

    func update() {
        guard let player, let assetId, let document, player.book?.assetId == assetId,
              let location = document.location(atMs: player.positionMs) else {
            if active { active = false }
            if playing { playing = false }
            if spoken != nil { spoken = nil }
            return
        }
        if !active { active = true }
        if playing != player.playing { playing = player.playing }
        let audio = document.chapters[location.chapter]
        if audioChapter != audio.index { audioChapter = audio.index }
        guard let chapter, chapter.index == audio.index, audio.sentences.indices.contains(location.sentence) else {
            if spoken != nil { spoken = nil }
            return
        }
        let sentence = audio.sentences[location.sentence]
        guard let paragraph = chapter.readerParagraph(textIndex: sentence.p) else { return }
        let text = chapter.paragraphs[paragraph]
        let starts = offsets[paragraph] ?? ReadingClock.tokenOffsets(text.text, tokens: text.tokens,
                                                                    base: audio.paragraphStart(sentence.p) ?? sentence.start)
        offsets[paragraph] = starts
        let offset = audio.offset(atMs: player.positionMs)
        let value = Spoken(chapter: chapter.index, paragraph: paragraph, token: starts.lastIndex { Double($0) <= offset })
        if value != spoken { spoken = value }
    }
}

/// Play/pause and ±15 s for the linked audiobook inside the reader.
struct ReaderAudioBar: View {
    @ObservedObject var player: AudioPlayerService
    let following: Bool
    /// Back to the spoken word: following and hiding the bars become the default again.
    let onFollow: () -> Void
    var body: some View {
        HStack(spacing: 30) {
            Button { player.jump(-15) } label: { Image(systemName: "gobackward.15").font(.title2) }.accessibilityLabel("Back 15 seconds")
            Button { if player.playing { player.pause() } else { player.resume() } } label: {
                Image(systemName: player.playing ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 42))
            }.accessibilityLabel(player.playing ? "Pause" : "Play")
            Button { player.jump(15) } label: { Image(systemName: "goforward.15").font(.title2) }.accessibilityLabel("Forward 15 seconds")
            Menu {
                ForEach([0.75, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3], id: \.self) { speed in
                    Button(String(format: "%g×", speed)) { player.setRate(Float(speed)) }
                }
            } label: { Text(String(format: "%g×", player.rate)).font(.callout.monospacedDigit().bold()).frame(minWidth: 44) }
                .accessibilityLabel("Playback speed")
            Button { onFollow() } label: {
                Image(systemName: following ? "location.fill" : "location").font(.title3)
            }.accessibilityLabel(following ? "Following the audio" : "Back to the audio")
        }
        .frame(maxWidth: .infinity).padding(.vertical, 6).background(PudgeTheme.panel.opacity(0.92))
    }
}

struct NovelReaderScreen: View {
    @ObservedObject var media: MediaModel
    let player: AudioPlayerService
    let bookId: Int
    let title: String
    @StateObject private var follower = ReaderAudioFollower()
    @State private var follow = true
    /// Bars hide only while the reader scrolls along with the audio and nobody touched it for 3 s.
    @State private var idleHidden = false
    @State private var interactions = 0
    /// Bumped by the arrow: scroll back to the spoken word even when already following.
    @State private var followRequest = 0
    /// A finger is on the text; following resumes unless it really scrolled.
    @State private var dragging = false
    @State private var dragStart: CGFloat?
    /// Blurred illustrations the reader has opened in this chapter.
    @State private var revealed = Set<Int>()
    @State private var illustration: IllustrationRoute?
    @State private var detail: LightNovelDetail?
    @State private var chapterIndex: Int?
    @State private var chapter: ChapterText?
    @State private var failure: String?
    /// Visible paragraphs; a reference so scrolling never re-renders the chapter.
    @State private var tracker = VisibleParagraphs()
    @State private var restoreParagraph: Int?
    @State private var lookup: WordLookup?
    @State private var finishedPrompt = false
    @State private var listenProblem: String?
    @State private var pendingListen: (paragraph: Int, utf16: Int)?
    @AppStorage("pudge.readerFontSize") private var fontSize = 21.0
    @AppStorage("pudge.readerMarkStyle") private var marks = "mac"
    @AppStorage("pudge.readerFurigana") private var furigana = "mac"

    init(media: MediaModel, bookId: Int, title: String) {
        self.media = media; self.player = media.app.player; self.bookId = bookId; self.title = title
    }

    private var chapterPosition: Int? { detail?.chapters.firstIndex { $0.index == chapterIndex } }
    private var autoScrolling: Bool { follower.active && follower.playing && follow }
    private var chrome: Bool { !(autoScrolling && idleHidden) }

    /// Any touch shows the bars again and restarts the 3-second timer.
    private func interacted() {
        interactions &+= 1
        media.app.activity.touched(kind: "light_novel", item: bookId)
        if idleHidden { withAnimation { idleHidden = false } }
    }

    /// The arrow in the audio bar: back to the spoken word, following on, bars hide again after 3 s.
    private func returnToAudio() {
        JapaneseTextView.clearActiveSelection()
        if follow { followRequest &+= 1 } else { follow = true }  // exactly one scroll back to the audio
        interacted()
    }

    private func scrollToAudio(_ proxy: ScrollViewProxy) {
        if let audio = follower.audioChapter, audio != chapterIndex, detail?.chapters.contains(where: { $0.index == audio }) == true {
            Task { await open(audio, fraction: 0) }
        } else if let paragraph = follower.spoken?.paragraph {
            withAnimation { proxy.scrollTo(paragraph, anchor: .center) }
        }
    }

    private func tappedEmpty() {
        // While following the audio a tap on the text hides the bars at once; otherwise they stay.
        if autoScrolling && !idleHidden { withAnimation { idleHidden = true } } else { interacted() }
    }
    private var style: ReaderStyle { ReaderStyle.make(media.readerLook, fontSize: fontSize, marks: marks, furigana: furigana) }
    private var background: Color { Color(uiColor: UIColor(hex: media.readerLook?.backgroundColor ?? "") ?? UIColor(PudgeTheme.background)) }
    private var audioBook: AudioManifest? {
        guard let id = detail?.audioAssetId else { return nil }
        return media.app.books.first { $0.assetId == id }
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: style.fontSize * 0.6) {
                    if let chapter {
                        Text(chapter.title).font(.title3.bold()).foregroundStyle(Color(uiColor: style.text)).padding(.bottom, 6)
                            .onTapGesture { tappedEmpty() }
                        if chapter.parsing {
                            Label("The Mac is splitting this chapter into words…", systemImage: "hourglass").font(.caption).foregroundStyle(.secondary)
                        }
                        ForEach(Array(chapter.paragraphs.enumerated()), id: \.offset) { index, paragraph in
                            Group {
                                if paragraph.image {
                                    let blurred = media.readerLook?.blurImages == true && !revealed.contains(index)
                                    ReaderIllustration(media: media, imageId: paragraph.imageId, blurred: blurred) {
                                        interacted()
                                        if blurred { withAnimation { _ = revealed.insert(index) } }
                                        else if let id = paragraph.imageId { illustration = IllustrationRoute(id: id) }
                                    }
                                } else {
                                    JapaneseText(paragraph: paragraph, style: style,
                                                 current: follower.spoken?.paragraph == index ? follower.spoken?.token : nil,
                                                 onTap: { token in interacted(); open(word: token, paragraph: index) },
                                                 onTapEmpty: { tappedEmpty() },
                                                 onSelect: { interacted(); if follower.active { follow = false } },
                                                 onListenFrom: detail?.audioAssetId == nil ? nil : { utf16 in listen(paragraph: index, utf16: utf16) })
                                }
                            }
                            .id(index)
                            .onAppear { tracker.visible.insert(index); remember() }
                            .onDisappear { tracker.visible.remove(index) }
                        }
                        footer
                    } else if let failure {
                        ContentUnavailableView("Could not open the chapter", systemImage: "wifi.slash", description: Text(failure))
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 80)
                    }
                }.padding(.horizontal, 20).padding(.vertical, 16)
                    // iPad: a readable column, centred (the iPhone is narrower than this anyway).
                    .frame(maxWidth: PudgeTheme.readingWidth).frame(maxWidth: .infinity)
            }
            .onScrollPhaseChange { old, phase, context in
                let offset = context.geometry.contentOffset.y
                if phase == .interacting {
                    dragging = true
                    dragStart = offset
                    interacted()
                } else if old == .interacting {
                    dragging = false
                    // Only a real scroll by hand stops following the audio (a touch or a tiny
                    // drag does not); the bars stay so the reader can use the arrow to go back.
                    if follower.active, abs(offset - (dragStart ?? offset)) > 24 { follow = false }
                    dragStart = nil
                }
            }
            .onChange(of: restoreParagraph) { _, value in
                guard let value else { return }
                DispatchQueue.main.async { proxy.scrollTo(value, anchor: .top); restoreParagraph = nil }
            }
            .onChange(of: follower.spoken?.paragraph) { _, value in
                guard follow, !dragging, let value else { return }
                DispatchQueue.main.async { withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(value, anchor: .center) } }
            }
            .onChange(of: follow) { _, value in
                if value { scrollToAudio(proxy) }
            }
            .onChange(of: followRequest) { _, _ in scrollToAudio(proxy) }
        }
        .background(background.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if follower.active && chrome {
                ReaderAudioBar(player: player, following: follow, onFollow: returnToAudio).simultaneousGesture(TapGesture().onEnded { interacted() })
            }
        }
        .toolbar(chrome ? .visible : .hidden, for: .navigationBar)
        .toolbar(.hidden, for: .tabBar)
        .statusBarHidden(!chrome)
        .task(id: ChromeTimer(interactions: interactions, autoScrolling: autoScrolling)) {
            guard autoScrolling else {
                if idleHidden { withAnimation { idleHidden = false } }
                return
            }
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled, autoScrolling else { return }
            withAnimation { idleHidden = true }
        }
        .onChange(of: follower.audioChapter) { _, value in
            // The audio moved to another chapter: the reader follows it.
            guard follow, let value, value != chapterIndex, detail?.chapters.contains(where: { $0.index == value }) == true else { return }
            Task { await open(value, fraction: 0) }
        }
        .fullScreenCover(item: $illustration) { item in IllustrationViewer(media: media, imageId: item.id) }
        .sheet(item: $lookup, onDismiss: {
            // After the word sheet is gone, so the alert can appear.
            if let target = pendingListen { pendingListen = nil; listen(paragraph: target.paragraph, utf16: target.utf16) }
        }) { item in WordSheet(media: media, lookup: item) }
        .alert("Listen", isPresented: Binding(get: { listenProblem != nil }, set: { if !$0 { DispatchQueue.main.async { listenProblem = nil } } })) {
            if let book = audioBook, (try? media.app.store.ready(book)) != true {
                Button("Download the audiobook") { media.app.download(book) }
            }
            Button("OK", role: .cancel) {}
        } message: { Text(listenProblem ?? "") }
        .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) { ReadingDownloadButton(media: media, kind: "ln", id: bookId, title: title) }
            if detail?.audioAssetId != nil && !follower.active {
                ToolbarItem(placement: .primaryAction) {
                    Button { listen(paragraph: tracker.visible.min() ?? 0, utf16: 0) } label: { Image(systemName: "headphones") }
                        .accessibilityLabel("Listen from here")
                }
            }
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Section("Text size") {
                        Button("Larger") { fontSize = min(36, fontSize + 2) }
                        Button("Smaller") { fontSize = max(14, fontSize - 2) }
                    }
                    Picker("Word marks", selection: $marks) {
                        Text("As on the Mac").tag("mac")
                        Text("Underline").tag("underline")
                        Text("Colour").tag("color")
                        Text("Highlight").tag("highlight")
                        Text("Off").tag("off")
                    }
                    Picker("Furigana", selection: $furigana) {
                        Text("As on the Mac").tag("mac")
                        Text("All words").tag("all")
                        Text("Off").tag("off")
                    }
                    if let detail {
                        Section("Chapters") {
                            ForEach(detail.chapters) { item in
                                Button(item.title.isEmpty ? "Chapter \(item.index + 1)" : item.title) { follow = false; Task { await open(item.index, fraction: 0) } }
                            }
                        }
                    }
                } label: { Image(systemName: "textformat.size") }
            }
        }
        .confirmationDialog("Mark this volume finished?", isPresented: $finishedPrompt, titleVisibility: .visible) {
            Button("Mark finished") { Task { await media.saveNovelPosition(bookId, chapter: chapterIndex ?? 0, offset: 1, finished: true) } }
        } message: { Text("The Mac updates the volume and AniList progress.") }
        .task {
            let look = Task { await media.loadReaderLook() }
            if media.readerLook == nil { await look.value }
            media.markOpened("ln", bookId)
            do {
                let value = try await media.novel(bookId)
                detail = value
                attachAudio()
                // With the audiobook playing, open the chapter being read aloud.
                if follower.active, let spoken = follower.audioChapter, value.chapters.contains(where: { $0.index == spoken }) {
                    await open(spoken, fraction: 0)
                } else {
                    let start = media.startPosition(bookId, value)
                    await open(start.chapter, fraction: start.offset)
                }
            } catch { failure = error.localizedDescription }
        }
        .task {
            // Statistics: what is on screen every 5 s (counts while touched in the last 5 min).
            // A cancelled sleep ends the loop: no observation after the screen closed.
            while true {
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
                observeReading()
            }
        }
        .onDisappear {
            save(force: true); player.embeddedControls = max(0, player.embeddedControls - 1)
            observeReading(); media.app.activity.stop(kind: "light_novel", item: bookId)
        }
        .onAppear { player.embeddedControls += 1 }
    }

    @ViewBuilder private var footer: some View {
        HStack {
            if let position = chapterPosition, position > 0, let previous = detail?.chapters[position - 1] {
                Button { follow = false; Task { await open(previous.index, fraction: 0) } } label: { Label("Previous", systemImage: "chevron.left") }
            }
            Spacer()
            if let position = chapterPosition, let detail, position + 1 < detail.chapters.count {
                let next = detail.chapters[position + 1]
                Button { follow = false; Task { await open(next.index, fraction: 0) } } label: { Label("Next chapter", systemImage: "chevron.right") }
                    .buttonStyle(.borderedProminent)
            } else {
                Button("Finish volume") { finishedPrompt = true }.buttonStyle(.borderedProminent)
            }
        }.padding(.top, 24).padding(.bottom, 40)
    }

    private func attachAudio() {
        let book = audioBook
        follower.attach(player: player, assetId: book?.assetId, document: book.flatMap { media.app.readAlong($0) })
    }

    private func open(word index: Int, paragraph number: Int) {
        guard let paragraph = chapter?.paragraphs[safe: number], let token = paragraph.tokens[safe: index] else { return }
        let surface = StudyText.segments(paragraph.text, tokens: paragraph.tokens).first { $0.token == index }?.text ?? ""
        var item = WordLookup(token: token, surface: surface, sentence: StudyText.sentence(paragraph.text, around: token))
        if detail?.audioAssetId != nil {
            item.playLabel = "Listen from here"
            item.playFromHere = { pendingListen = (number, token.start); lookup = nil }
        }
        lookup = item
    }

    /// Plays the linked audiobook from a reader position; the reader then follows the audio.
    private func listen(paragraph number: Int, utf16: Int) {
        guard let book = audioBook else {
            listenProblem = "The linked audiobook is not in the \(PudgeTheme.device) library yet. Open Audiobooks and refresh."; return
        }
        guard (try? media.app.store.ready(book)) == true else {
            listenProblem = "Download “\(book.title)” to the \(PudgeTheme.device) first."; return
        }
        guard let document = media.app.readAlong(book), let chapter, let index = chapterIndex,
              let audio = document.chapters.first(where: { $0.index == index }) else {
            listenProblem = "This chapter is not aligned with the audiobook yet."; return
        }
        let offset = audio.offset(paragraph: chapter.textParagraph(number), utf16: utf16) ?? audio.sentences.first?.start ?? 0
        let time = audio.timeMs(atOffset: offset)
        Task {
            if player.book?.assetId == book.assetId {
                try? await player.seek(time); player.resume()
            } else {
                await media.app.play(book, position: time)
            }
            follow = true
            attachAudio()
        }
    }

    private func open(_ index: Int, fraction: Double) async {
        save(force: true)
        chapterIndex = index; chapter = nil; failure = nil; tracker.visible = []; tracker.lastSaved = Date(); revealed = []
        follower.chapter = nil
        do {
            var restored = false
            try await media.chapter(bookId, index: index) { value in
                guard chapterIndex == index else { return }  // the reader moved to another chapter
                chapter = value
                follower.chapter = value
                if !restored && !(follow && follower.active) { restored = true; restoreParagraph = value.paragraph(atFraction: fraction) }
            }
        } catch { if chapterIndex == index && chapter == nil { failure = error.localizedDescription } }
        // Fresh Jiten states (the chapter's parse cache keeps the states of its parse time).
        guard chapterIndex == index, let tokens = chapter?.paragraphs.flatMap(\.tokens), !tokens.isEmpty else { return }
        let states = await media.liveStates(tokens)
        guard chapterIndex == index, !states.isEmpty, var value = chapter else { return }
        for row in value.paragraphs.indices { value.paragraphs[row].tokens = value.paragraphs[row].tokens.updating(states) }
        chapter = value
        follower.chapter = value
    }

    private func remember() {
        // A paragraph scrolled into view by the reader (following the audio is not reading activity, as on the Mac).
        if !autoScrolling { media.app.activity.touched(kind: "light_novel", item: bookId) }
        if Date().timeIntervalSince(tracker.lastSaved) > 8 { save(force: false) }
    }

    private func observeReading() {
        guard let chapter, let index = chapterIndex, let top = tracker.visible.min() else { return }
        let activity = media.app.activity
        activity.observe(kind: "light_novel", item: bookId, active: activity.isReading(kind: "light_novel", item: bookId),
                         at: ["chapter": Double(index), "fraction": chapter.fraction(atParagraph: top)])
    }

    private func save(force: Bool) {
        guard let chapter, let index = chapterIndex, let top = tracker.visible.min() else { return }
        tracker.lastSaved = Date()
        let fraction = chapter.fraction(atParagraph: top)
        Task { await media.saveNovelPosition(bookId, chapter: index, offset: fraction) }
    }
}

private struct ChromeTimer: Equatable { let interactions: Int; let autoScrolling: Bool }

struct IllustrationRoute: Identifiable { let id: String }

/// A light-novel illustration from the Mac; blurred like the Mac reader's until tapped.
struct ReaderIllustration: View {
    @ObservedObject var media: MediaModel
    let imageId: String?
    let blurred: Bool
    let onTap: () -> Void
    @State private var image: UIImage?
    @State private var failed = false
    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFit()
                    .frame(maxWidth: .infinity, maxHeight: 560)
                    .blur(radius: blurred ? 36 : 0)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .overlay {
                        if blurred {
                            Text("Show illustration").font(.footnote.bold()).foregroundStyle(.white)
                                .padding(.horizontal, 14).padding(.vertical, 8)
                                .background(Color.black.opacity(0.62), in: Capsule())
                        }
                    }
            } else if failed || imageId == nil {
                Label("Illustration", systemImage: "photo").font(.caption).foregroundStyle(.secondary)
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 120)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .contentShape(Rectangle())
        .onTapGesture { onTap() }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(blurred ? "Hidden illustration" : "Illustration")
        .accessibilityAddTraits(.isButton)
        .task(id: imageId) {
            guard let imageId else { return }
            image = await media.image("ln-image/\(imageId)")
            failed = image == nil
        }
    }
}

/// An illustration on its own: pinch or double-tap to zoom, tap or × to close.
struct IllustrationViewer: View {
    @ObservedObject var media: MediaModel
    let imageId: String
    @Environment(\.dismiss) private var dismiss
    @State private var image: UIImage?
    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            if let image {
                ZoomableContainer(onTap: { _, _ in dismiss() }) {
                    Image(uiImage: image).resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                .ignoresSafeArea()
            } else {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Button { dismiss() } label: {
                Image(systemName: "xmark.circle.fill").font(.title).symbolRenderingMode(.hierarchical).foregroundStyle(.white)
            }
            .padding()
            .accessibilityLabel("Close")
        }
        .task { image = await media.image("ln-image/\(imageId)") }
    }
}

final class VisibleParagraphs {
    var visible = Set<Int>()
    var lastSaved = Date.distantPast
}

// MARK: Manga reader

struct MangaReaderScreen: View {
    @ObservedObject var media: MediaModel
    let book: MangaItem
    @State private var page = 0
    @State private var showText = true
    @State private var lookup: TextLookup?
    @State private var chrome = true
    @Environment(\.dismiss) private var dismiss

    private var order: [Int] { book.rightToLeft ? Array((0..<book.pages).reversed()) : Array(0..<book.pages) }

    var body: some View {
        TabView(selection: $page) {
            ForEach(order, id: \.self) { index in
                MangaPageView(media: media, book: book.id, page: index, showText: showText, active: index == page,
                              onRegion: { text in media.app.activity.touched(kind: "manga", item: book.id); lookup = TextLookup(text: text) },
                              onBackground: { media.app.activity.touched(kind: "manga", item: book.id); withAnimation { chrome.toggle() } })
                    .tag(index)
            }
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .background(Color.black.ignoresSafeArea())
        .toolbar(chrome ? .visible : .hidden, for: .navigationBar)
        .toolbar(.hidden, for: .tabBar)
        .navigationTitle("\(page + 1) / \(book.pages)").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) { ReadingDownloadButton(media: media, kind: "manga", id: book.id, title: book.title) }
            ToolbarItem(placement: .primaryAction) { GetOCRButton(media: media, book: book) }
            ToolbarItem(placement: .primaryAction) {
                Button { showText.toggle() } label: { Image(systemName: showText ? "text.viewfinder" : "viewfinder") }
                    .accessibilityLabel(showText ? "Hide text boxes" : "Show text boxes")
            }
        }
        .sheet(item: $lookup) { item in TextLookupSheet(media: media, lookup: item) }
        .onAppear {
            page = min(max(0, media.startPage(book)), max(0, book.pages - 1)); media.app.activity.touched(kind: "manga", item: book.id)
            media.markOpened("manga", book.id)
        }
        .onDisappear {
            observeReading(); media.app.activity.stop(kind: "manga", item: book.id)
            Task { await media.refreshReading() }
        }
        .task {
            // Statistics: the page on screen every 5 s (counts while touched in the last 5 min).
            // A cancelled sleep ends the loop: no observation after the screen closed.
            while true {
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
                observeReading()
            }
        }
        .onChange(of: page) { _, value in
            media.app.activity.touched(kind: "manga", item: book.id)
            Task {
                try? await Task.sleep(nanoseconds: 800_000_000)
                if value == page { await media.saveMangaPosition(book.id, page: value) }
            }
        }
    }

    private func observeReading() {
        let activity = media.app.activity
        activity.observe(kind: "manga", item: book.id, active: activity.isReading(kind: "manga", item: book.id), at: ["page": Double(page)])
    }
}

struct MangaPageView: View {
    @ObservedObject var media: MediaModel
    let book: Int
    let page: Int
    let showText: Bool
    /// The page on screen; a page swiped away goes back to its normal size.
    var active = true
    let onRegion: (String) -> Void
    /// A tap outside the text boxes (shows or hides the reader's bars).
    var onBackground: () -> Void = {}
    @State private var image: UIImage?
    @State private var text: MangaPageText?
    @State private var failed = false
    var body: some View {
        Group {
            if let image {
                // Pinch or double-tap to zoom; while zoomed one finger pans, unzoomed it turns pages.
                ZoomableContainer(active: active, onTap: { point, size in tapped(at: point, in: size, image: image) }) {
                    GeometryReader { geometry in
                        let frame = fit(image.size, in: geometry.size)
                        ZStack {
                            Image(uiImage: image).resizable().frame(width: frame.width, height: frame.height)
                                .position(x: frame.midX, y: frame.midY)
                            if showText, let regions = text?.regions {
                                ForEach(Array(regions.enumerated()), id: \.offset) { _, region in
                                    let box = rect(region, in: frame)
                                    Rectangle().fill(PudgeTheme.accent.opacity(0.12))
                                        .overlay(Rectangle().stroke(PudgeTheme.accent.opacity(0.7), lineWidth: 1))
                                        .frame(width: box.width, height: box.height)
                                        .position(x: box.midX, y: box.midY)
                                        .accessibilityElement()
                                        .accessibilityLabel(region.text)
                                        .accessibilityAddTraits(.isButton)
                                        .accessibilityAction { onRegion(region.text) }
                                }
                            }
                        }
                    }
                }
            } else if failed {
                ContentUnavailableView("Page unavailable", systemImage: "wifi.slash", description: Text("Download the volume to read it without the Mac."))
                    .contentShape(Rectangle()).onTapGesture { onBackground() }
            } else {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
                    .contentShape(Rectangle()).onTapGesture { onBackground() }
            }
        }
        .task(id: page) {
            image = await media.image("manga/\(book)/pages/\(page)")
            failed = image == nil
            text = await media.mangaText(book, page: page)
        }
    }

    /// The smallest text box under the finger opens; elsewhere the tap toggles the bars.
    private func tapped(at point: CGPoint, in size: CGSize, image: UIImage) {
        let frame = fit(image.size, in: size)
        if showText, let regions = text?.regions {
            let hits = regions.filter { rect($0, in: frame).insetBy(dx: -4, dy: -4).contains(point) }
            if let region = hits.min(by: { $0.width * $0.height < $1.width * $1.height }) {
                onRegion(region.text)
                return
            }
        }
        onBackground()
    }

    private func rect(_ region: MangaRegion, in frame: CGRect) -> CGRect {
        let width = max(14, region.width * frame.width), height = max(14, region.height * frame.height)
        let centerX = frame.minX + (region.x + region.width / 2) * frame.width
        let centerY = frame.minY + (region.y + region.height / 2) * frame.height
        return CGRect(x: centerX - width / 2, y: centerY - height / 2, width: width, height: height)
    }

    private func fit(_ size: CGSize, in box: CGSize) -> CGRect {
        guard size.width > 0, size.height > 0 else { return CGRect(origin: .zero, size: box) }
        let scale = min(box.width / size.width, box.height / size.height)
        let width = size.width * scale, height = size.height * scale
        return CGRect(x: (box.width - width) / 2, y: (box.height - height) / 2, width: width, height: height)
    }
}

// MARK: Pinch zoom

/// SwiftUI content in a zooming UIScrollView: pinch or double-tap to zoom, one finger pans while
/// zoomed. Unzoomed, the content fits exactly, so a surrounding page view still turns pages.
struct ZoomableContainer<Content: View>: UIViewRepresentable {
    var maxScale: CGFloat
    /// false puts the content back to its normal size.
    var active: Bool
    /// A single tap, in the content's unzoomed coordinates, with the content's size.
    var onTap: ((CGPoint, CGSize) -> Void)?
    let content: Content

    init(maxScale: CGFloat = 5, active: Bool = true, onTap: ((CGPoint, CGSize) -> Void)? = nil,
         @ViewBuilder content: () -> Content) {
        self.maxScale = maxScale; self.active = active; self.onTap = onTap; self.content = content()
    }

    func makeCoordinator() -> Coordinator {
        let hosting = UIHostingController(rootView: content)
        hosting.safeAreaRegions = []
        hosting.view.backgroundColor = .clear
        return Coordinator(hosting: hosting)
    }

    func makeUIView(context: Context) -> ZoomScrollView {
        let scroll = ZoomScrollView()
        scroll.delegate = context.coordinator
        scroll.minimumZoomScale = 1
        scroll.maximumZoomScale = maxScale
        scroll.bouncesZoom = true
        scroll.showsHorizontalScrollIndicator = false
        scroll.showsVerticalScrollIndicator = false
        scroll.contentInsetAdjustmentBehavior = .never
        scroll.backgroundColor = .clear
        let hosted: UIView = context.coordinator.hosting.view
        scroll.addSubview(hosted)
        scroll.zoomed = hosted
        let double = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.doubleTapped(_:)))
        double.numberOfTapsRequired = 2
        let single = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped(_:)))
        single.require(toFail: double)
        scroll.addGestureRecognizer(double)
        scroll.addGestureRecognizer(single)
        context.coordinator.scroll = scroll
        context.coordinator.onTap = onTap
        return scroll
    }

    func updateUIView(_ scroll: ZoomScrollView, context: Context) {
        context.coordinator.hosting.rootView = content
        context.coordinator.onTap = onTap
        scroll.maximumZoomScale = maxScale
        if !active && scroll.zoomScale > 1.001 { scroll.setZoomScale(1, animated: false) }
    }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        let hosting: UIHostingController<Content>
        weak var scroll: ZoomScrollView?
        var onTap: ((CGPoint, CGSize) -> Void)?

        init(hosting: UIHostingController<Content>) { self.hosting = hosting }

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { hosting.view }

        @objc func tapped(_ recognizer: UITapGestureRecognizer) {
            guard let view = hosting.view else { return }
            onTap?(recognizer.location(in: view), view.bounds.size)
        }

        @objc func doubleTapped(_ recognizer: UITapGestureRecognizer) {
            guard let scroll, let view = hosting.view else { return }
            if scroll.zoomScale > 1.01 {
                scroll.setZoomScale(1, animated: true)
                return
            }
            let point = recognizer.location(in: view)
            let size = CGSize(width: view.bounds.width / 2.5, height: view.bounds.height / 2.5)
            scroll.zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2,
                                   width: size.width, height: size.height), animated: true)
        }
    }
}

/// Keeps the unzoomed content exactly the size of the scroll view.
final class ZoomScrollView: UIScrollView {
    weak var zoomed: UIView?
    override func layoutSubviews() {
        super.layoutSubviews()
        guard let zoomed, abs(zoomScale - 1) < 0.001, zoomed.bounds.size != bounds.size || contentSize != bounds.size else { return }
        zoomed.frame = CGRect(origin: .zero, size: bounds.size)
        contentSize = bounds.size
    }
}
