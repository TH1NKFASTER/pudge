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

/// Light novels from the Mac, grouped by franchise → series → volumes like the Mac library,
/// as a grid of cover cards (one per shelf; a shelf of several volumes opens its volumes).
struct NovelsScreen: View {
    @ObservedObject var media: MediaModel
    /// The pull-down header: search (title, series, franchise) and "Only on iPhone/iPad".
    @State private var filter = LibraryFilter()
    @State private var path = NavigationPath()
    @State private var width: CGFloat = 390
    var shelves: [Shelf<LightNovelItem>] {
        let items = media.novels.filter {
            filter.matches([$0.title, $0.seriesTitle, $0.franchiseTitle], onDevice: media.isDownloaded("ln", $0.id))
        }
        return Shelves.group(items, franchise: { $0.franchiseInfo }, series: { $0.seriesInfo },
                             volume: { $0.volume ?? 0 }, updated: { $0.updatedAt })
    }
    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if let error = media.readingError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
                    ReadingNote(media: media)
                    if shelves.isEmpty && !media.loadingReading {
                        if filter.isActive {
                            ContentUnavailableView.search(text: filter.search)
                        } else {
                            ContentUnavailableView("No light novels", systemImage: "book", description: Text("Light novels from the Mac's library appear here. Download a volume to read it without the Mac."))
                        }
                    }
                    let layout = LibraryLayout(width: width)
                    LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                        ForEach(shelves) { shelf in
                            let items = shelf.items
                            if items.count == 1, let book = items.first {
                                NovelCard(media: media, book: book, showTitle: true, width: layout.card) { path.append(NovelRoute(id: book.id, title: book.title)) }
                            } else {
                                NovelShelfCard(media: media, shelf: shelf, width: layout.card,
                                               open: { path.append(ShelfRoute(key: shelf.key)) },
                                               read: { book in path.append(NovelRoute(id: book.id, title: book.title)) })
                            }
                        }
                    }.libraryGridIdentity(layout)
                }
                .padding(.horizontal, CGFloat(LibraryGrid.padding)).padding(.bottom, 24)
            }
            .libraryWidth($width)
            .background(PudgeTheme.background)
            .navigationTitle("Light Novels").libraryFilterHeader($filter, prompt: "Title", device: PudgeTheme.device)
            .navigationDestination(for: NovelRoute.self) { route in NovelReaderScreen(media: media, bookId: route.id, title: route.title) }
            .navigationDestination(for: ShelfRoute.self) { route in
                if let shelf = shelves.first(where: { $0.key == route.key }) {
                    NovelShelfScreen(media: media, shelf: shelf) { book in path.append(NovelRoute(id: book.id, title: book.title)) }
                }
            }
            .toolbar { ToolbarItem(placement: .primaryAction) { SettingsButton(model: media.app) } }
            .overlay { if media.loadingReading && media.novels.isEmpty { ProgressView() } }
            .refreshable { await media.refreshReading(forceUpdates: true) }
            // Not loaded from the Mac yet (the volumes on this device may be listed meanwhile).
            .task { if !media.novelsFromMac { await media.refreshReading() } }
            .popsToRootOnReselect($path)
        }
    }
}

/// A light-novel volume as a cover card; ⋯ has its download menu.
struct NovelCard: View {
    @ObservedObject var media: MediaModel
    let book: LightNovelItem
    var showTitle = false
    let width: CGFloat
    let open: () -> Void
    var body: some View {
        // The Mac's lnBookCard: alone (showTitle) its title and chips; inside a series "Volume N" and the compact chips.
        LibraryCard(title: showTitle || book.volume == nil ? book.title : "Volume \(book.volume!)",
                    titleSymbol: book.hasAudio ? "headphones" : nil,
                    status: CardStatusText.novel(volume: book.volume, characters: book.characters, year: book.year,
                                                 progress: book.progress, finished: book.finished, compact: !showTitle,
                                                 download: media.cardDownload("ln", book.id)),
                    badge: media.cardBadge("ln", book.id), width: width, identifier: "ln-card", open: open) {
            CardCover(media: media, path: "cover/ln/\(book.id)", symbol: "book")
        } actions: {
            Button(action: open) { Label(book.started && !book.finished ? "Continue reading" : "Read", systemImage: "book.fill") }
            ReadingDownloadMenu(media: media, kind: "ln", id: book.id, title: book.title)
            DeletedOnMacNote(media: media, kind: "ln", id: book.id)
        }
    }
}

/// A franchise or series of several volumes: the volume the Mac would continue on the cover.
struct NovelShelfCard: View {
    @ObservedObject var media: MediaModel
    let shelf: Shelf<LightNovelItem>
    let width: CGFloat
    let open: () -> Void
    let read: (LightNovelItem) -> Void
    var body: some View {
        let items = shelf.items
        let target = macCurrent(shelf, finished: { $0.finished })
        // The Mac: a franchise tile ("Volume 3 · 42%", "Franchise · 2 series") or a series card's head.
        let status = shelf.series.count > 1
            ? CardStatusText.franchise(series: shelf.series.count, currentVolume: target?.volume, currentProgress: target?.progress,
                                       currentFinished: target?.finished ?? true, allFinished: items.allSatisfy(\.finished))
            : CardStatusText.series(volumes: items.count, continueVolume: target.flatMap { $0.finished ? nil : ($0.volume ?? 1) })
        LibraryCard(title: shelf.title,
                    status: status,
                    badge: media.cardBadge("ln", ids: items.map(\.id)), count: items.count, width: width,
                    identifier: "ln-shelf-card", open: open) {
            CardCover(media: media, path: "cover/ln/\((target ?? items[0]).id)", symbol: "book")
        } actions: {
            if let target, !target.finished {
                Button { read(target) } label: {
                    Label("Continue " + (target.volume.map { "vol. \($0)" } ?? target.title), systemImage: "book.fill")
                }
            }
            Button(action: open) { Label("All \(items.count) volumes", systemImage: "square.grid.2x2") }
            if let target {
                ReadingDownloadMenu(media: media, kind: "ln", id: target.id, title: target.title)
            }
        }
    }
}

/// Volumes of one franchise or series, a grid per series.
struct NovelShelfScreen: View {
    @ObservedObject var media: MediaModel
    let shelf: Shelf<LightNovelItem>
    let read: (LightNovelItem) -> Void
    @State private var width: CGFloat = 390
    var body: some View {
        let layout = LibraryLayout(width: width)
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(shelf.series) { series in
                    if shelf.series.count > 1 { LibrarySectionHeader(title: series.title, count: series.items.count) }
                    LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                        ForEach(series.items) { book in NovelCard(media: media, book: book, width: layout.card) { read(book) } }
                    }.libraryGridIdentity(layout)
                }
            }
            .padding(.horizontal, CGFloat(LibraryGrid.padding)).padding(.vertical, 12)
        }
        .libraryWidth($width)
        .background(PudgeTheme.background)
        .japaneseNavigationTitle(shelf.title)
    }
}

/// Manga grouped by series → volumes like the Mac library, as a grid of cover cards.
struct MangaScreen: View {
    @ObservedObject var media: MediaModel
    /// The pull-down header: search (title, series) and "Only on iPhone/iPad".
    @State private var filter = LibraryFilter()
    @State private var path = NavigationPath()
    @State private var width: CGFloat = 390
    var shelves: [Shelf<MangaItem>] {
        let items = media.manga.filter {
            filter.matches([$0.title, $0.seriesTitle], onDevice: media.isDownloaded("manga", $0.id))
        }
        return Shelves.group(items, franchise: { _ in nil }, series: { ($0.seriesKey ?? "book:\($0.id)", $0.seriesTitle ?? $0.title) },
                             volume: { $0.volume ?? 0 }, updated: { $0.updatedAt })
    }
    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if let error = media.readingError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
                    ReadingNote(media: media)
                    if shelves.isEmpty && !media.loadingReading {
                        if filter.isActive {
                            ContentUnavailableView.search(text: filter.search)
                        } else {
                            ContentUnavailableView("No manga", systemImage: "book.pages", description: Text("Manga from the Mac's library appear here. Download a volume to read it without the Mac."))
                        }
                    }
                    let layout = LibraryLayout(width: width)
                    LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                        ForEach(shelves) { shelf in
                            let items = shelf.items
                            if items.count == 1, let book = items.first {
                                MangaCard(media: media, book: book, showTitle: true, width: layout.card) { path.append(MangaRoute(id: book.id)) }
                            } else {
                                MangaShelfCard(media: media, shelf: shelf, width: layout.card,
                                               open: { path.append(ShelfRoute(key: shelf.key)) },
                                               read: { book in path.append(MangaRoute(id: book.id)) })
                            }
                        }
                    }.libraryGridIdentity(layout)
                }
                .padding(.horizontal, CGFloat(LibraryGrid.padding)).padding(.bottom, 24)
            }
            .libraryWidth($width)
            .background(PudgeTheme.background)
            .navigationTitle("Manga").libraryFilterHeader($filter, prompt: "Title", device: PudgeTheme.device)
            .navigationDestination(for: MangaRoute.self) { route in
                if let book = media.manga.first(where: { $0.id == route.id }) { MangaReaderScreen(media: media, book: book) }
            }
            .navigationDestination(for: ShelfRoute.self) { route in
                if let shelf = shelves.first(where: { $0.key == route.key }) {
                    MangaShelfScreen(media: media, shelf: shelf) { book in path.append(MangaRoute(id: book.id)) }
                }
            }
            .toolbar { ToolbarItem(placement: .primaryAction) { SettingsButton(model: media.app) } }
            .overlay { if media.loadingReading && media.manga.isEmpty { ProgressView() } }
            .refreshable { await media.refreshReading(forceUpdates: true) }
            .task { if media.manga.isEmpty { await media.refreshReading() } }
            .popsToRootOnReselect($path)
        }
    }
}

extension MangaItem {
    var isRead: Bool { pages > 0 && readPages >= pages }
}

/// A manga volume as a cover card; ⋯ has its download menu and Get OCR.
struct MangaCard: View {
    @ObservedObject var media: MediaModel
    let book: MangaItem
    var showTitle = false
    let width: CGFloat
    let open: () -> Void
    var body: some View {
        // The Mac's mangaLibraryCard: alone its chips; inside a series "Volume N" and the compact chips.
        LibraryCard(title: showTitle || book.volume == nil ? book.title : "Volume \(book.volume!)",
                    status: CardStatusText.manga(volume: book.volume, pages: book.pages, readPages: book.readPages,
                                                 compact: !showTitle, download: media.cardDownload("manga", book.id)),
                    badge: media.cardBadge("manga", book.id), width: width, identifier: "manga-card", open: open) {
            CardCover(media: media, path: "cover/manga/\(book.id)", symbol: "book.pages")
        } actions: {
            Button(action: open) { Label(book.position > 0 && !book.isRead ? "Continue reading" : "Read", systemImage: "book.pages.fill") }
            ReadingDownloadMenu(media: media, kind: "manga", id: book.id, title: book.title)
            if media.needsOCR(book) { GetOCRMenuItem(media: media, book: book) }
            DeletedOnMacNote(media: media, kind: "manga", id: book.id)
        }
    }
}

/// A manga series of several volumes.
struct MangaShelfCard: View {
    @ObservedObject var media: MediaModel
    let shelf: Shelf<MangaItem>
    let width: CGFloat
    let open: () -> Void
    let read: (MangaItem) -> Void
    var body: some View {
        let items = shelf.items
        let target = macCurrent(shelf, finished: { $0.isRead })
        LibraryCard(title: shelf.title,
                    status: CardStatusText.series(volumes: items.count,
                                                  continueVolume: target.flatMap { $0.isRead ? nil : ($0.volume ?? 1) }),
                    badge: media.cardBadge("manga", ids: items.map(\.id)), count: items.count, width: width,
                    identifier: "manga-shelf-card", open: open) {
            CardCover(media: media, path: "cover/manga/\((target ?? items[0]).id)", symbol: "book.pages")
        } actions: {
            if let target, !target.isRead {
                Button { read(target) } label: { Label("Continue vol. \(target.volume ?? 1)", systemImage: "book.pages.fill") }
            }
            Button(action: open) { Label("All \(items.count) volumes", systemImage: "square.grid.2x2") }
            if let target {
                ReadingDownloadMenu(media: media, kind: "manga", id: target.id, title: target.title)
                if media.needsOCR(target) { GetOCRMenuItem(media: media, book: target) }
            }
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
    let read: (MangaItem) -> Void
    @State private var width: CGFloat = 390
    var body: some View {
        let layout = LibraryLayout(width: width)
        ScrollView {
            LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                ForEach(shelf.items) { book in MangaCard(media: media, book: book, width: layout.card) { read(book) } }
            }.libraryGridIdentity(layout)
            .padding(.horizontal, CGFloat(LibraryGrid.padding)).padding(.vertical, 12)
        }
        .libraryWidth($width)
        .background(PudgeTheme.background)
        .japaneseNavigationTitle(shelf.title)
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
    /// What "Explain grammar" in the word card explains: the word's sentence and the text before it.
    @State private var lookupSource: GrammarSource?
    /// The grammar chat of this volume (hidden and shown again with its button).
    @StateObject private var grammar = GrammarChatModel()
    @State private var finishedPrompt = false
    @State private var listenProblem: String?
    @State private var pendingListen: (paragraph: Int, utf16: Int)?
    /// The volume's "read up to here" marker (pudge/ln_read_marker.py), its line once drawn,
    /// whether its paragraph is on screen, the label a tap shows, and a pulse when it moves.
    @State private var marker: ReadMarker?
    @State private var markerLine: MarkerLine?
    /// The spoken word's line once drawn: following the audio keeps this line in the middle.
    @State private var spokenLine: SpokenLine?
    @State private var markerShown = false
    @State private var markerNote: String?
    @State private var markerFlash = 0
    @State private var markerAudioAt = Date.distantPast
    /// The text selection over the chapter's paragraphs (handles, menu, across paragraphs).
    @State private var textSelection = TextSelectionController(acrossParagraphs: true)
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
            if spokenLine?.paragraph == paragraph, tracker.visible.contains(paragraph) {
                withAnimation { proxy.scrollTo(SpokenLineAnchor.id, anchor: .center) }
            } else {
                // Off screen: brought in without animation (a lazy stack only estimates the rows
                // it has not drawn), then centred on its line once that is drawn.
                proxy.scrollTo(paragraph, anchor: .center)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                    guard spokenLine?.paragraph == paragraph else { return }
                    withAnimation { proxy.scrollTo(SpokenLineAnchor.id, anchor: .center) }
                }
            }
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
                                    // One selection over the chapter's paragraphs (textSelection).
                                    JapaneseText(paragraph: paragraph, style: style,
                                                 current: follower.spoken?.paragraph == index ? follower.spoken?.token : nil,
                                                 onTap: { token in interacted(); open(word: token, paragraph: index) },
                                                 onTapEmpty: { tappedEmpty() },
                                                 marker: markerOffset(index),
                                                 onMarkerLine: { line in placeMarker(index, line) },
                                                 selection: textSelection, paragraphIndex: index,
                                                 onCurrentLine: { line in placeSpokenLine(index, line) })
                                }
                            }
                            // The marker's ribbon in the margin by its line; a tap says who set it and when.
                            .overlay(alignment: .topLeading) {
                                if let line = markerLine, line.paragraph == index, markerOffset(index) != nil {
                                    ReadMarkerRibbon(flash: markerFlash)
                                        .frame(width: 22, height: max(14, line.rect.height))
                                        .offset(x: -24, y: line.rect.minY)
                                        .onTapGesture { interacted(); showMarkerNote() }
                                        .accessibilityLabel(markerLabel)
                                        .accessibilityAddTraits(.isButton)
                                }
                            }
                            // Where the spoken line is in this paragraph: following the audio centres
                            // the line itself (a paragraph can be taller than the screen).
                            .background(alignment: .topLeading) {
                                if let line = spokenLine, line.paragraph == index {
                                    Color.clear.frame(width: 1, height: max(1, line.rect.height))
                                        .id(SpokenLineAnchor.id)
                                        .padding(.top, max(0, line.rect.minY))
                                        .allowsHitTesting(false)
                                }
                            }
                            .id(index)
                            .onAppear { tracker.visible.insert(index); remember(); if isMarkerParagraph(index) { markerShown = true } }
                            .onDisappear { tracker.visible.remove(index); if isMarkerParagraph(index) { markerShown = false } }
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
            .modifier(ManualScrollWatch(began: {
                dragging = true
                interacted()
            }, ended: { moved in
                dragging = false
                // Only a real scroll by hand stops following the audio (a touch or a tiny
                // drag does not); the bars stay so the reader can use the arrow to go back.
                if follower.active, moved > 24 { follow = false }
            }))
            .onChange(of: restoreParagraph) { _, value in
                guard let value else { return }
                DispatchQueue.main.async { proxy.scrollTo(value, anchor: .top); restoreParagraph = nil }
            }
            .onChange(of: follower.spoken?.paragraph) { _, value in
                guard follow, !dragging, let value else { return }
                if follower.spoken?.token == nil {
                    // No word to follow (sentence timing only): the paragraph, as before.
                    DispatchQueue.main.async { withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(value, anchor: .center) } }
                    return
                }
                // The spoken line is centred when its paragraph has drawn it (spokenLine below). A
                // paragraph off screen is brought in first, without animation: a lazy stack only
                // estimates the rows it has not drawn, and an animated scroll there landed beside
                // the line and jerked back.
                if !tracker.visible.contains(value) { DispatchQueue.main.async { proxy.scrollTo(value, anchor: .center) } }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                    // No line drawn (a word without a box): the paragraph, as before.
                    guard follow, !dragging, follower.spoken?.paragraph == value, spokenLine?.paragraph != value else { return }
                    withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(value, anchor: .center) }
                }
            }
            .onChange(of: spokenLine) { _, line in
                // Once per line, not per word, and onto the line itself: in a paragraph taller than
                // the screen the spoken line no longer runs out of view, and each step is one line.
                guard follow, !dragging, let line, line.paragraph == follower.spoken?.paragraph else { return }
                DispatchQueue.main.async { withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(SpokenLineAnchor.id, anchor: .center) } }
            }
            .onChange(of: chrome) { _, _ in
                // The bars coming or going move the text by their height: the line back to the middle.
                guard autoScrolling, !dragging, let line = spokenLine, line.paragraph == follower.spoken?.paragraph else { return }
                DispatchQueue.main.async { withAnimation(.easeInOut(duration: 0.3)) { proxy.scrollTo(SpokenLineAnchor.id, anchor: .center) } }
            }
            .onChange(of: follow) { _, value in
                if value { scrollToAudio(proxy) }
            }
            .onChange(of: followRequest) { _, _ in scrollToAudio(proxy) }
            .onChange(of: follower.spoken) { _, value in followMarker(value) }
            // Back to the marker when it is off screen (like the audio bar's arrow back to the audio).
            .overlay(alignment: .bottomTrailing) {
                if marker != nil, chapter != nil, !markerShown {
                    Button { jumpToMarker(proxy) } label: {
                        Label("Back to bookmark", systemImage: "bookmark.fill").labelStyle(.iconOnly)
                            .font(.title3).foregroundStyle(Color.red).frame(width: 44, height: 44)
                            .background(PudgeTheme.panel.opacity(0.92), in: Circle())
                    }
                    .padding(.trailing, 16).padding(.bottom, 12)
                    .accessibilityLabel("Back to bookmark")
                }
            }
            // The hidden grammar chat comes back with this button.
            .overlay(alignment: .bottomLeading) { GrammarChatButton(chats: grammar) { lookup = nil } }
            .overlay(alignment: .top) {
                if let markerNote {
                    Text(markerNote).font(.footnote).padding(.horizontal, 12).padding(.vertical, 7)
                        .background(.thinMaterial, in: Capsule()).padding(.top, 8)
                        .transition(.opacity).allowsHitTesting(false)
                }
            }
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
        // A word graded in the word card: every occurrence in the chapter takes its new state at once.
        .onChange(of: media.gradedWords) { _, graded in
            guard let value = chapter?.updating(graded.rows) else { return }
            chapter = value
            follower.chapter = value
        }
        .onChange(of: follower.audioChapter) { _, value in
            // The audio moved to another chapter: the reader follows it.
            guard follow, let value, value != chapterIndex, detail?.chapters.contains(where: { $0.index == value }) == true else { return }
            Task { await open(value, fraction: 0) }
        }
        .fullScreenCover(item: $illustration) { item in IllustrationViewer(media: media, imageId: item.id) }
        // The grammar chat and the word card open small at the bottom: the text stays readable.
        // A tap outside the word card only closes it; around the chat the text stays usable. The
        // word card comes after the chat, so a word tapped while the chat is open shows on top of
        // it (the chat's button closes the card).
        .bottomPanel(isPresented: $grammar.shown, title: "Explain grammar", compact: 380, keyboard: true) {
            GrammarChatPanel(chats: grammar, media: media)
        }
        .bottomPanel(item: $lookup, onDismiss: {
            // After the word card is gone.
            if let target = pendingListen { pendingListen = nil; listen(paragraph: target.paragraph, utf16: target.utf16) }
        }) { item in WordSheet(media: media, lookup: item, onExplain: explainAction(lookupSource)) }
        .alert("Listen", isPresented: Binding(get: { listenProblem != nil }, set: { if !$0 { DispatchQueue.main.async { listenProblem = nil } } })) {
            if let book = audioBook, (try? media.app.store.ready(book)) != true {
                Button("Download the audiobook") { media.app.download(book) }
            }
            Button("OK", role: .cancel) {}
        } message: { Text(listenProblem ?? "") }
        .japaneseNavigationTitle(title)
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
                marker = value.marker
                attachAudio()
                // With the audiobook playing, open the chapter being read aloud.
                if follower.active, let spoken = follower.audioChapter, value.chapters.contains(where: { $0.index == spoken }) {
                    await open(spoken, fraction: 0)
                } else {
                    let start = media.startPosition(bookId, value)
                    await open(start.chapter, fraction: start.offset, initial: true)
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
            textSelection.forgetActions()  // its closures hold this screen's state
            grammar.stop()
        }
        .onAppear {
            player.embeddedControls += 1; configureSelection()
            grammar.open(book: GrammarChat.book(server: media.app.paired?.serverId, kind: "ln", id: bookId))
        }
    }

    /// The selection menu over the chapter: Copy (its paragraphs' text), Look Up (the first word
    /// with an entry), Listen from here and Read up to here at the selection's start.
    private func configureSelection() {
        textSelection.paragraph = { index in chapter?.paragraphs[safe: index] }
        textSelection.onSelect = { interacted(); if follower.active { follow = false } }
        textSelection.onLookUp = { word in interacted(); open(word: word.token, paragraph: word.paragraph) }
        textSelection.canListen = { detail?.audioAssetId != nil }
        textSelection.onListenFrom = { place in listen(paragraph: place.paragraph, utf16: place.offset) }
        textSelection.onMarkHere = { place in markHere(paragraph: place.paragraph, utf16: place.offset) }
        textSelection.onExplain = { range in interacted(); explain(selection: range) }
    }

    /// "Explain grammar" of the selected text, with the 200 characters before it (as on the Mac).
    private func explain(selection range: TextSelectionRange) {
        let paragraph: (Int) -> TextParagraph? = { index in chapter?.paragraphs[safe: index] }
        explain(GrammarSource(sentence: range.text(paragraph), context: GrammarContext.preceding(paragraph, before: range.start)))
    }

    private func explain(_ source: GrammarSource) {
        lookup = nil
        grammar.explain(source, media: media)
    }

    private func explainAction(_ source: GrammarSource?) -> (() -> Void)? {
        guard let source else { return nil }
        return { explain(source) }
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
        let sentenceStart = TextUnits.sentence(paragraph.text, at: token.start)?.lowerBound ?? 0
        lookupSource = GrammarSource(sentence: item.sentence,
                                     context: GrammarContext.preceding({ index in chapter?.paragraphs[safe: index] },
                                                                       before: TextPosition(paragraph: number, offset: sentenceStart)))
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

    /// `initial`: the place the volume opens at (saving it again is not news); `target`: the
    /// paragraph to show instead of the fraction's.
    private func open(_ index: Int, fraction: Double, initial: Bool = false, paragraph target: Int? = nil) async {
        save(force: true)
        textSelection.clear()  // its places are this chapter's paragraphs
        chapterIndex = index; chapter = nil; failure = nil; tracker.visible = []; tracker.lastSaved = Date(); revealed = []; spokenLine = nil
        markerShown = false; markerLine = nil
        follower.chapter = nil
        do {
            var restored = false
            try await media.chapter(bookId, index: index) { value in
                guard chapterIndex == index else { return }  // the reader moved to another chapter
                chapter = value
                follower.chapter = value
                if !restored && !(follow && follower.active) {
                    restored = true
                    let paragraph = target ?? value.paragraph(atFraction: fraction)
                    restoreParagraph = paragraph
                    if initial { tracker.opened = OpenedPlace(chapter: index, position: Double(paragraph), tolerance: 2) }
                }
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
        // Still where the volume opened (two paragraphs either way): nothing new. Saved with a fresh
        // time, the old place of a copy opened offline took a later place read on the Mac back.
        if var opened = tracker.opened {
            let moved = opened.counts(chapter: index, position: Double(top))
            tracker.opened = opened
            if !moved { return }
        }
        tracker.lastSaved = Date()
        let fraction = chapter.fraction(atParagraph: top)
        // The Mac moves the read marker by the paragraph at the top (forward only, far enough).
        Task { updateMarker(await media.saveNovelPosition(bookId, chapter: index, offset: fraction, paragraph: top)) }
    }

    // MARK: Read marker

    private func isMarkerParagraph(_ index: Int) -> Bool {
        guard let marker, let chapterIndex else { return false }
        return marker.chapter == chapterIndex && marker.paragraph == index
    }

    private func markerOffset(_ index: Int) -> Int? { isMarkerParagraph(index) ? marker?.paragraphOffset : nil }

    private func placeSpokenLine(_ index: Int, _ line: CGRect?) {
        if let line { spokenLine = SpokenLine(paragraph: index, rect: line) }
        else if spokenLine?.paragraph == index { spokenLine = nil }
    }

    private func placeMarker(_ index: Int, _ line: CGRect?) {
        if let line { markerLine = MarkerLine(paragraph: index, rect: line) }
        else if markerLine?.paragraph == index { markerLine = nil }
    }

    /// The Mac's marker after a save; a pulse when it moved.
    private func updateMarker(_ value: ReadMarker?) {
        guard let value else { return }
        let moved = marker?.chapter != value.chapter || marker?.paragraph != value.paragraph || marker?.paragraphOffset != value.paragraphOffset
        marker = value
        markerShown = value.chapter == chapterIndex && tracker.visible.contains(value.paragraph)
        if moved { markerFlash &+= 1 }
    }

    private var markerLabel: String { marker.map { markerText($0) } ?? "Read up to here" }

    private func markerText(_ value: ReadMarker) -> String {
        let kind = value.kind == "manual" ? "set by you" : "automatic"
        let time = Date(timeIntervalSince1970: value.at).formatted(date: .omitted, time: .shortened)
        let device = value.here == true ? "" : (value.deviceName ?? "")
        return "Read up to here · \(kind) · \(time)" + (device.isEmpty ? "" : " · \(device)")
    }

    /// The label after a tap on the ribbon, for a moment (there is no hover on the phone).
    private func showMarkerNote() {
        guard let marker else { return }
        let text = markerText(marker)
        withAnimation { markerNote = text }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            if markerNote == text { withAnimation { markerNote = nil } }
        }
    }

    /// "Read up to here" in the selection menu: exactly at the selection's first character.
    private func markHere(paragraph number: Int, utf16: Int) {
        guard let index = chapterIndex else { return }
        interacted()
        Task {
            let saved = await media.saveReadMarker(bookId, chapter: index, paragraph: number, utf16: utf16, kind: "manual")
            // Without the Mac it shows here until the volume opens again.
            updateMarker(saved ?? ReadMarker(chapter: index, paragraph: number, paragraphOffset: utf16, kind: "manual",
                                             at: Date().timeIntervalSince1970, here: true))
            markerFlash &+= 1
            showMarkerNote()
        }
    }

    /// While the linked audiobook plays, the marker follows the word being read (the Mac moves it
    /// only forward, far enough, to the sentence start); at most every 15 s.
    private func followMarker(_ spoken: ReaderAudioFollower.Spoken?) {
        // "Auto-bookmark after 20 s" off on the Mac: only Read up to here moves the marker.
        guard media.readerLook?.autoBookmarks != false else { return }
        guard let spoken, follower.playing, Date().timeIntervalSince(markerAudioAt) > 15 else { return }
        if let marker, marker.chapter > spoken.chapter || (marker.chapter == spoken.chapter && marker.paragraph > spoken.paragraph) { return }
        markerAudioAt = Date()
        let utf16 = spoken.token.flatMap { chapter?.paragraphs[safe: spoken.paragraph]?.tokens[safe: $0]?.start } ?? 0
        Task { updateMarker(await media.saveReadMarker(bookId, chapter: spoken.chapter, paragraph: spoken.paragraph, utf16: utf16, kind: "auto")) }
    }

    private func jumpToMarker(_ proxy: ScrollViewProxy) {
        guard let marker else { return }
        follow = false
        interacted()
        markerFlash &+= 1
        if marker.chapter == chapterIndex {
            withAnimation { proxy.scrollTo(marker.paragraph, anchor: .center) }
        } else if detail?.chapters.contains(where: { $0.index == marker.chapter }) != false {
            // Another chapter (or the volume's chapters not loaded yet): open it at the marker,
            // never that paragraph number of this chapter.
            Task { await open(marker.chapter, fraction: 0, paragraph: marker.paragraph) }
        }
    }
}

private struct MarkerLine: Equatable { let paragraph: Int; let rect: CGRect }
/// The spoken word's line in its paragraph, and the id of the zero-width view on it.
private struct SpokenLine: Equatable { let paragraph: Int; let rect: CGRect }
private enum SpokenLineAnchor { static let id = "pudge.reader.spokenLine" }

/// The red ribbon of the read marker in the margin; it pulses when the marker moves.
struct ReadMarkerRibbon: View {
    let flash: Int
    @State private var glow = false
    var body: some View {
        ZStack(alignment: .trailing) {
            Color.clear
            RoundedRectangle(cornerRadius: 2).fill(Color.red).frame(width: 6)
                .shadow(color: Color.red.opacity(glow ? 0.9 : 0), radius: glow ? 7 : 0)
                .scaleEffect(glow ? 1.3 : 1)
        }
        .contentShape(Rectangle())
        .onChange(of: flash) { _, _ in
            withAnimation(.easeOut(duration: 0.2)) { glow = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { withAnimation(.easeOut(duration: 1.1)) { glow = false } }
        }
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
    /// Where the volume opened (its paragraph), until the reader moves away.
    var opened: OpenedPlace?
}

// MARK: Manga reader

struct MangaReaderScreen: View {
    @ObservedObject var media: MediaModel
    let book: MangaItem
    /// The place and what is on screen (MangaReadingPlace, PudgeCore): only a turn moves the
    /// place and saves it; turning the device or pairing the pages anew only shows the spread
    /// that holds it.
    @State private var place = MangaReadingPlace(page: 0, layout: [])
    /// Text box outlines on the page (kept between volumes); hidden boxes open on a long press.
    @AppStorage("pudge.mangaShowTextBoxes") private var showText = true
    @State private var lookup: TextLookup?
    /// The grammar chat of this volume (hidden and shown again with its button).
    @StateObject private var grammar = GrammarChatModel()
    @State private var chrome = true
    /// The reader's whole size (bars and safe areas included, so showing the bars changes nothing).
    @State private var screen = CGSize.zero
    /// Pages that are one wide picture (a double-page spread): shown alone.
    @State private var wide: Set<Int> = []
    /// height / width of the volume's usual page.
    @State private var pageAspect = MangaSpreads.defaultAspect
    @State private var aspectKnown = false
    /// "Shift pairs by one page" for this volume (kept on the Mac, as on the Mac).
    @State private var shift = false
    /// "Align scanned pages" for this volume and the pages' transforms (the Mac works them out).
    @State private var align = true
    /// "Align scanned pages" just turned on: the alignment is asked once more (see the loop).
    @State private var alignAskPending = false
    @State private var alignment: MangaAlignment?
    @Environment(\.dismiss) private var dismiss

    /// Two pages side by side like a printed book when they fit (landscape iPhone/iPad, an unfolded
    /// display): by the space and the page shape, not the device. Each half then gets one page and
    /// the gutter is the middle of the reader, where a fold is.
    private var twoPages: Bool {
        MangaSpreads.showsTwoPages(width: Double(screen.width), height: Double(screen.height), pageAspect: pageAspect)
    }
    /// The pager from left to right: right to left (manga) starts at the right.
    private var order: [[Int]] { book.rightToLeft ? Array(place.layout.reversed()) : place.layout }

    var body: some View {
        GeometryReader { geometry in
            let whole = CGSize(width: geometry.size.width + geometry.safeAreaInsets.leading + geometry.safeAreaInsets.trailing,
                               height: geometry.size.height + geometry.safeAreaInsets.top + geometry.safeAreaInsets.bottom)
            let generation = place.generation
            TabView(selection: Binding(get: { place.shown }, set: { tag in turn(to: tag, generation: generation) })) {
                ForEach(order, id: \.self) { spread in
                    MangaPageView(media: media, book: book.id, pages: MangaSpreads.onScreen(spread, rightToLeft: book.rightToLeft),
                                  showText: showText, active: spread.first == place.shown,
                                  onRegion: { text in media.app.activity.touched(kind: "manga", item: book.id); lookup = TextLookup(text: text) },
                                  onBackground: { media.app.activity.touched(kind: "manga", item: book.id); withAnimation { chrome.toggle() } },
                                  onImage: { index, size in learn(index, size) }, alignment: transforms(spread))
                        .tag(spread.first ?? 0)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            // A new layout gets a new pager: the old one never reports a page of the old layout.
            .id(generation)
            .onAppear { screen = whole; relayout() }
            .onChange(of: whole) { _, value in screen = value; relayout() }
        }
        .background(Color.black.ignoresSafeArea())
        .toolbar(chrome ? .visible : .hidden, for: .navigationBar)
        .toolbar(.hidden, for: .tabBar)
        .navigationTitle(MangaSpreads.title(place.shownPages, pageCount: book.pages)).navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) { ReadingDownloadButton(media: media, kind: "manga", id: book.id, title: book.title) }
            ToolbarItem(placement: .primaryAction) { GetOCRButton(media: media, book: book) }
            ToolbarItem(placement: .primaryAction) {
                Button { showText.toggle() } label: { Image(systemName: showText ? "text.viewfinder" : "viewfinder") }
                    .accessibilityLabel(showText ? "Hide text box outlines" : "Show text box outlines")
            }
            ToolbarItem(placement: .primaryAction) {
                if twoPages {
                    Button { toggleShift() } label: { Image(systemName: shift ? "rectangle.split.2x1.fill" : "rectangle.split.2x1") }
                        .accessibilityLabel("Shift pairs by one page")
                        .accessibilityValue(shift ? "On" : "Off")
                }
            }
            ToolbarItem(placement: .primaryAction) {
                if twoPages {
                    Button { toggleAlign() } label: { Image(systemName: align ? "align.vertical.center.fill" : "align.vertical.center") }
                        .accessibilityLabel("Align scanned pages")
                        .accessibilityValue(align ? "On" : "Off")
                }
            }
        }
        // The hidden grammar chat comes back with this button (closing Look up, so it is on top).
        .overlay(alignment: .bottomLeading) { GrammarChatButton(chats: grammar) { lookup = nil } }
        // The grammar chat and Look up open small at the bottom: the page stays in view. Look up
        // comes after the chat, so a bubble tapped while the chat is open shows on top of it.
        .bottomPanel(isPresented: $grammar.shown, title: "Explain grammar", compact: 380, keyboard: true) {
            GrammarChatPanel(chats: grammar, media: media)
        }
        .bottomPanel(item: $lookup, title: "Look up") { item in
            TextLookupSheet(media: media, lookup: item, onExplain: { text in explainBubble(text) })
        }
        .onAppear {
            grammar.open(book: GrammarChat.book(server: media.app.paired?.serverId, kind: "manga", id: book.id))
            let start = min(max(0, media.startPage(book)), max(0, book.pages - 1))
            shift = media.mangaPairShift(book.id)
            align = media.mangaPageAlign(book.id)
            place = MangaReadingPlace(page: start, layout: layoutNow())
            media.app.activity.touched(kind: "manga", item: book.id)
            media.markOpened("manga", book.id)
        }
        .onDisappear {
            observeReading(); media.app.activity.stop(kind: "manga", item: book.id)
            grammar.stop()
            Task { await media.refreshReading() }
        }
        .task {
            // Double-page spreads, the page shape and the shift: from the downloaded files or the Mac.
            guard let known = await media.mangaSpreadPages(book.id) else { return }
            wide.formUnion(known.wide)
            if let aspect = known.aspect, aspect.isFinite, aspect > 0 { pageAspect = aspect; aspectKnown = true }
            if let value = known.shift { shift = value }
            if let value = known.align { align = value }
            alignment = known.alignment
            relayout()
        }
        .task {
            // The Mac works the alignment out in the background: ask again only while it does
            // (only while aligning: with it off the Mac works nothing out), and once after
            // "Align scanned pages" was turned on here. Nothing pending: no request at all (it
            // asked every 30 s for a change made on the Mac; reopening the volume shows that).
            while true {
                do { try await Task.sleep(for: .seconds(3)) } catch { break }
                let working = align && ((alignment?.pending ?? 0) > 0 || alignAskPending)
                guard twoPages, working else { continue }
                alignAskPending = false
                await refreshSpreads()
            }
        }
        .task {
            // Statistics: the pages on screen every 5 s (counts while touched in the last 5 min).
            // A cancelled sleep ends the loop: no observation after the screen closed.
            while true {
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
                observeReading()
            }
        }
    }

    /// "Explain grammar" of a text box (or a word's sentence in it), with the text of the pages
    /// on screen as its context (the Mac sends its page's text).
    /// The chat opens at once ("Explaining…") while the pages' text comes; without the Mac only
    /// the box itself is sent (the chat then says the Mac is away).
    private func explainBubble(_ text: String) {
        lookup = nil
        let pages = place.shownPages
        let volume = book.id
        grammar.explain(GrammarSource(sentence: text), media: media) {
            var texts: [String] = []
            for page in pages {
                texts += (await media.mangaText(volume, page: page))?.regions.map(\.text) ?? []
            }
            return GrammarContext.joined(texts)
        }
    }

    /// What one turn shows now: the space, the spreads known and the shift.
    private func layoutNow() -> [[Int]] {
        MangaSpreads.spreads(pageCount: book.pages, wide: wide, twoPages: twoPages, shift: shift)
    }

    /// Rotation, a spread found, the pairs shifted: the same place, in the spread that holds it.
    private func relayout() { place.relayout(layoutNow()) }

    /// The pager settled on a spread: a turn is saved (the first page; a pair's second page is
    /// read too). Anything else the pager reports (the same spread, an old pager) is not.
    private func turn(to tag: Int, generation: Int) {
        guard let save = place.turned(to: tag, generation: generation) else { return }
        media.app.activity.touched(kind: "manga", item: book.id)
        Task {
            try? await Task.sleep(nanoseconds: 800_000_000)
            if place.page == save.page { await media.saveMangaPosition(book.id, page: save.page, readThrough: save.readThrough) }
        }
    }

    private func toggleShift() {
        shift.toggle()
        relayout()
        let value = shift
        Task { await media.setMangaPairShift(book.id, value) }
    }

    /// "Align scanned pages": shown at once, saved on the Mac (later when it does not answer).
    private func toggleAlign() {
        align.toggle()
        let value = align
        Task { await media.setMangaPageAlign(book.id, value) }
        // Turned on: the Mac starts working it out; asked once for its `pending`.
        alignAskPending = value
    }

    /// The transforms of a spread's pages left to right on the screen (none: drawn as they are).
    private func transforms(_ spread: [Int]) -> [MangaPageTransform?] {
        guard align, twoPages, let found = alignment?.transforms(for: spread, rightToLeft: book.rightToLeft) else { return [] }
        return book.rightToLeft ? Array(found.reversed()) : found
    }

    /// The Mac's answer again: the alignment as far as it is worked out, and a shift, alignment
    /// or wide page changed there, shown at the same place.
    private func refreshSpreads() async {
        guard let known = await media.mangaSpreadPages(book.id) else { return }
        if known.alignment != alignment { alignment = known.alignment }
        if let value = known.align, value != align { align = value }
        var changed = false
        if let value = known.shift, value != shift { shift = value; changed = true }
        if !Set(known.wide).isSubset(of: wide) { wide.formUnion(known.wide); changed = true }
        if changed { relayout() }
    }

    /// The pages on screen: both of a pair (each counts once in Statistics).
    private func observeReading() {
        let activity = media.app.activity
        var at: [String: Double] = ["page": Double(place.shown)]
        if place.shownPages.count > 1, let last = place.shownPages.last { at["pageEnd"] = Double(last) }
        activity.observe(kind: "manga", item: book.id, active: activity.isReading(kind: "manga", item: book.id), at: at)
    }

    /// A page's picture arrived: a wide one is shown alone from now on; the first other one gives
    /// the page shape when the Mac did not (an older Mac).
    private func learn(_ index: Int, _ size: CGSize) {
        let width = Double(size.width), height = Double(size.height)
        if MangaSpreads.isWide(width: width, height: height) {
            guard !wide.contains(index) else { return }
            wide.insert(index)
        } else if !aspectKnown, width > 0, height > 0 {
            pageAspect = height / width; aspectKnown = true
        } else {
            return
        }
        relayout()
    }
}

struct MangaPageView: View {
    @ObservedObject var media: MediaModel
    let book: Int
    /// The pages shown together, left to right on the screen: one page, or two side by side.
    let pages: [Int]
    let showText: Bool
    /// The page on screen; a page swiped away goes back to its normal size.
    var active = true
    let onRegion: (String) -> Void
    /// A tap on the page, not on a shown text box (shows or hides the reader's bars).
    var onBackground: () -> Void = {}
    /// A page's picture arrived, with its size (wide pages are then shown alone).
    var onImage: (Int, CGSize) -> Void = { _, _ in }
    /// How each page is drawn (left to right as `pages`; empty or nil: as it is): straightened
    /// and aligned with its facing page, its text boxes with it (MangaPageTransform).
    var alignment: [MangaPageTransform?] = []
    @State private var images: [Int: UIImage] = [:]
    @State private var texts: [Int: MangaPageText] = [:]
    @State private var loaded = false
    var body: some View {
        Group {
            if !images.isEmpty {
                // Pinch or double-tap to zoom (two pages zoom together); while zoomed one finger
                // pans, unzoomed it turns pages. Text boxes: a tap opens one while their outlines
                // show, a long press always, on either page.
                ZoomableContainer(active: active, onTap: { point, size in tapped(at: point, in: size) },
                                  onLongPress: { point, size in pressed(at: point, in: size) }) {
                    GeometryReader { geometry in
                        let layout = frames(in: geometry.size)
                        ZStack {
                            ForEach(Array(pages.enumerated()), id: \.offset) { slot, index in
                                if let image = images[index], slot < layout.count {
                                    let frame = layout[slot]
                                    let aligned = pageTransform(slot)
                                    Image(uiImage: image).resizable().frame(width: frame.width, height: frame.height)
                                        .modifier(MangaAlignedPage(transform: aligned, size: frame.size))
                                        .position(x: frame.midX, y: frame.midY)
                                    // Hidden outlines: the boxes stay (clear) for VoiceOver; a long press opens them.
                                    if let regions = texts[index]?.regions {
                                        ForEach(Array(regions.enumerated()), id: \.offset) { _, region in
                                            let box = rect(region, in: frame)
                                            // On an aligned page the box turns and moves with it.
                                            let centre = aligned?.screenPoint(CGPoint(x: box.midX, y: box.midY), in: frame)
                                                ?? CGPoint(x: box.midX, y: box.midY)
                                            Rectangle().fill(showText ? PudgeTheme.accent.opacity(0.12) : Color.clear)
                                                .overlay(Rectangle().stroke(showText ? PudgeTheme.accent.opacity(0.7) : Color.clear, lineWidth: 1))
                                                .frame(width: box.width, height: box.height)
                                                .scaleEffect(CGFloat(aligned?.safeScale ?? 1))
                                                .rotationEffect(.degrees(aligned?.angle ?? 0))
                                                .position(x: centre.x, y: centre.y)
                                                .accessibilityElement()
                                                .accessibilityLabel(region.text)
                                                .accessibilityAddTraits(.isButton)
                                                .accessibilityAction { onRegion(region.text) }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            } else if loaded {
                ContentUnavailableView("Page unavailable", systemImage: "wifi.slash", description: Text("Download the volume to read it without the Mac."))
                    .contentShape(Rectangle()).onTapGesture { onBackground() }
            } else {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
                    .contentShape(Rectangle()).onTapGesture { onBackground() }
            }
        }
        .task(id: pages) {
            // The pictures first (shown as soon as one is here), then their text boxes.
            for index in pages where images[index] == nil {
                if let image = await media.image("manga/\(book)/pages/\(index)") {
                    images[index] = image
                    onImage(index, image.size)
                }
            }
            loaded = true
            for index in pages where texts[index] == nil {
                texts[index] = await media.mangaText(book, page: index)
            }
        }
    }

    /// Where each page goes: one page fits the view; two meet in the middle (MangaSpreads.frames).
    /// A page whose picture did not come takes its half as a blank.
    private func frames(in size: CGSize) -> [CGRect] {
        // An aligned pair is drawn at one height (its transforms are made for that).
        let sameHeight = pages.count == 2 && alignment.contains { $0 != nil }
        return MangaSpreads.frames(pages.map { images[$0]?.size ?? .zero }, in: size, sameHeight: sameHeight)
    }

    /// The transform of the page in `slot` (left to right), nil: drawn as it is.
    private func pageTransform(_ slot: Int) -> MangaPageTransform? {
        slot < alignment.count ? alignment[slot] : nil
    }

    /// With the outlines shown, the text box under the finger opens; otherwise (and elsewhere)
    /// the tap is a tap on the page (bars).
    private func tapped(at point: CGPoint, in size: CGSize) {
        if showText, let region = region(at: point, in: size) {
            onRegion(region.text)
            return
        }
        onBackground()
    }

    /// A long press opens the text box under the finger, outlines shown or hidden.
    private func pressed(at point: CGPoint, in size: CGSize) {
        guard let region = region(at: point, in: size) else { return }
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        onRegion(region.text)
    }

    /// The smallest text box under the point (a few points of slack), on whichever page it is.
    private func region(at point: CGPoint, in size: CGSize) -> MangaRegion? {
        let layout = frames(in: size)
        var hits: [MangaRegion] = []
        for (slot, index) in pages.enumerated() where images[index] != nil && slot < layout.count {
            let frame = layout[slot]
            // An aligned page: the point on the page as drawn (and nothing past its gutter).
            let aligned = pageTransform(slot)
            if let aligned, !aligned.keeps(point, in: frame) { continue }
            let onPage = aligned?.pagePoint(point, in: frame) ?? point
            hits += (texts[index]?.regions ?? []).filter { rect($0, in: frame).insetBy(dx: -4, dy: -4).contains(onPage) }
        }
        return hits.min(by: { $0.width * $0.height < $1.width * $1.height })
    }

    private func rect(_ region: MangaRegion, in frame: CGRect) -> CGRect {
        let width = max(14, region.width * frame.width), height = max(14, region.height * frame.height)
        let centerX = frame.minX + (region.x + region.width / 2) * frame.width
        let centerY = frame.minY + (region.y + region.height / 2) * frame.height
        return CGRect(x: centerX - width / 2, y: centerY - height / 2, width: width, height: height)
    }
}

/// A manga page drawn with its alignment (MangaPageTransform): turned and scaled around its
/// centre and moved, cut off at the gutter line, its backing painted behind it.
struct MangaAlignedPage: ViewModifier {
    let transform: MangaPageTransform?
    let size: CGSize

    @ViewBuilder
    func body(content: Content) -> some View {
        if let transform {
            content
                .scaleEffect(CGFloat(transform.safeScale))
                .rotationEffect(.degrees(transform.angle))
                .offset(x: CGFloat(transform.dx) * size.width, y: CGFloat(transform.dy) * size.height)
                .clipShape(MangaGutterClip(gutter: transform.gutter))
                .background(alignment: .topLeading) { MangaPageBacking(transform: transform, size: size) }
        } else {
            content
        }
    }
}

/// Everything on the page's side of the gutter line (all of it for a page shown alone).
struct MangaGutterClip: Shape {
    let gutter: String?

    func path(in rect: CGRect) -> Path {
        let room = max(rect.width, rect.height)
        var area = rect.insetBy(dx: -room, dy: -room)
        if gutter == "right" {
            area.size.width = rect.maxX - area.minX
        } else if gutter == "left" {
            area = CGRect(x: rect.minX, y: area.minY, width: area.maxX - rect.minX, height: area.height)
        }
        return Path(area)
    }
}

/// The band along the gutter a straightened page may leave open, in the page's own colour there.
struct MangaPageBacking: View {
    let transform: MangaPageTransform
    let size: CGSize

    var body: some View {
        if let box = transform.backingRect(in: size) {
            let rgb = transform.fillComponents
            Rectangle().fill(Color(red: rgb[0], green: rgb[1], blue: rgb[2]))
                .frame(width: box.width, height: box.height)
                .offset(x: box.minX, y: box.minY)
        }
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
    /// A long press (0.4 s, the finger still), the same way; never while zooming or panning.
    var onLongPress: ((CGPoint, CGSize) -> Void)?
    let content: Content

    init(maxScale: CGFloat = 5, active: Bool = true, onTap: ((CGPoint, CGSize) -> Void)? = nil,
         onLongPress: ((CGPoint, CGSize) -> Void)? = nil, @ViewBuilder content: () -> Content) {
        self.maxScale = maxScale; self.active = active; self.onTap = onTap; self.onLongPress = onLongPress; self.content = content()
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
        // A finger that moves first pans (allowableMovement), two fingers pinch: neither is a long press.
        let long = UILongPressGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.longPressed(_:)))
        long.minimumPressDuration = 0.4
        long.allowableMovement = 10
        long.delegate = context.coordinator
        scroll.addGestureRecognizer(double)
        scroll.addGestureRecognizer(single)
        scroll.addGestureRecognizer(long)
        context.coordinator.scroll = scroll
        context.coordinator.onTap = onTap
        context.coordinator.onLongPress = onLongPress
        return scroll
    }

    func updateUIView(_ scroll: ZoomScrollView, context: Context) {
        context.coordinator.hosting.rootView = content
        context.coordinator.onTap = onTap
        context.coordinator.onLongPress = onLongPress
        scroll.maximumZoomScale = maxScale
        if !active && scroll.zoomScale > 1.001 { scroll.setZoomScale(1, animated: false) }
    }

    final class Coordinator: NSObject, UIScrollViewDelegate, UIGestureRecognizerDelegate {
        let hosting: UIHostingController<Content>
        weak var scroll: ZoomScrollView?
        var onTap: ((CGPoint, CGSize) -> Void)?
        var onLongPress: ((CGPoint, CGSize) -> Void)?

        init(hosting: UIHostingController<Content>) { self.hosting = hosting }

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { hosting.view }

        @objc func tapped(_ recognizer: UITapGestureRecognizer) {
            guard let view = hosting.view else { return }
            onTap?(recognizer.location(in: view), view.bounds.size)
        }

        @objc func longPressed(_ recognizer: UILongPressGestureRecognizer) {
            guard recognizer.state == .began, let view = hosting.view else { return }
            onLongPress?(recognizer.location(in: view), view.bounds.size)
        }

        /// The long press only where one is wanted, and never during a zoom, pan or fling.
        func gestureRecognizerShouldBegin(_ recognizer: UIGestureRecognizer) -> Bool {
            guard recognizer is UILongPressGestureRecognizer else { return true }
            guard onLongPress != nil, let scroll else { return false }
            return !(scroll.isZooming || scroll.isDragging || scroll.isDecelerating)
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


/// Tells when the reader starts scrolling by hand (`began`) and how far the scroll moved when the
/// finger lifts (`ended`). iOS 18 reads the scroll view's own phases; iOS 17 (older iPads stop
/// there) has no scroll phases, so a drag gesture alongside the scroll view stands in.
private struct ManualScrollWatch: ViewModifier {
    let began: () -> Void
    let ended: (CGFloat) -> Void
    @State private var start: CGFloat?

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.onScrollPhaseChange { old, phase, context in
                let offset = context.geometry.contentOffset.y
                if phase == .interacting {
                    start = offset
                    began()
                } else if old == .interacting {
                    ended(abs(offset - (start ?? offset)))
                    start = nil
                }
            }
        } else {
            content.simultaneousGesture(
                DragGesture(minimumDistance: 8)
                    .onChanged { _ in
                        if start == nil {
                            start = 0
                            began()
                        }
                    }
                    .onEnded { value in
                        ended(abs(value.translation.height))
                        start = nil
                    }
            )
        }
    }
}
