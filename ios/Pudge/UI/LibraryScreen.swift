import SwiftUI
import UniformTypeIdentifiers
import PudgeCore

struct PudgeTheme {
    /// "iPhone" or "iPad": texts name the device Pudge runs on (several can be paired with one Mac).
    @MainActor static var device: String { UIDevice.current.model }
    /// Longest comfortable line for reading text on an iPad (points).
    static let readingWidth: CGFloat = 820
    static let background = Color(red: 11/255, green: 19/255, blue: 32/255)
    static let panel = Color(red: 17/255, green: 29/255, blue: 45/255)
    static let accent = Color(red: 111/255, green: 140/255, blue: 1)
    static let good = Color(red: 87/255, green: 211/255, blue: 140/255)
    static func time(_ milliseconds: Int64) -> String {
        let seconds = max(0, milliseconds / 1000)
        if seconds >= 3600 { return String(format: "%lld:%02lld:%02lld", seconds/3600, seconds/60%60, seconds%60) }
        return String(format: "%lld:%02lld", seconds/60, seconds%60)
    }
    static func size(_ bytes: Int64) -> String { ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file) }
    /// A Storage category's size: "—" when nothing is stored (not "Zero KB").
    static func storageSize(_ bytes: Int64) -> String { StorageSizeText.text(bytes, formatted: size) }
}

struct LibraryScreen: View {
    @ObservedObject var model: PudgeModel
    /// The pull-down header: search (title, series) and "Only on iPhone/iPad" (libraryFilterHeader).
    @State private var filter = LibraryFilter()
    @State private var importing = false
    @AppStorage("pudge.librarySort") private var sortByRecent = true
    @State private var width: CGFloat = 390
    @State private var path = NavigationPath()
    /// The Mac's lazy catalog: never on this device.
    var pending: [PendingBook] {
        model.pendingBooks.filter { filter.matches([$0.title, $0.seriesTitle], onDevice: false) }
    }
    var filtered: [AudioManifest] {
        model.books.filter { filter.matches([$0.title, $0.seriesTitle], onDevice: (try? model.store.ready($0)) == true) }
    }
    /// Volumes of one series stay together (as on the Mac), with its volumes still on the Mac;
    /// groups are ordered by the most recently listened volume, or by title.
    var groups: [BookGroup] {
        let grouped = Dictionary(grouping: filtered) { BookGroup.series($0.title, $0.seriesTitle) }
        let waiting = Dictionary(grouping: pending) { BookGroup.series($0.title, $0.seriesTitle) }
        let items = grouped.map { key, value in
            BookGroup(title: key, books: value.sorted { ($0.volume ?? 0, $0.title) < ($1.volume ?? 0, $1.title) },
                      pending: BookGroup.ordered(waiting[key] ?? []))
        }
        func recent(_ books: [AudioManifest]) -> Double { books.map { model.lastPlayed[$0.assetId] ?? 0 }.max() ?? 0 }
        return items.sorted { left, right in
            if sortByRecent {
                let a = recent(left.books), b = recent(right.books)
                if a != b { return a > b }
            }
            return left.title.localizedStandardCompare(right.title) == .orderedAscending
        }
    }
    /// Books only on the Mac whose series has nothing above: one card per series (a flat list of
    /// one long title each could not tell volume 1 from volume 3).
    var pendingGroups: [BookGroup] {
        let listed = Set(filtered.map { BookGroup.series($0.title, $0.seriesTitle) })
        let grouped = Dictionary(grouping: pending.filter { !listed.contains(BookGroup.series($0.title, $0.seriesTitle)) }) {
            BookGroup.series($0.title, $0.seriesTitle)
        }
        return grouped.map { key, value in BookGroup(title: key, books: [], pending: BookGroup.ordered(value)) }
            .sorted { $0.title.localizedStandardCompare($1.title) == .orderedAscending }
    }
    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    // No Mac-available line and no filter toggle here: "Only on iPhone/iPad" is in
                    // the pull-down header with the search (as on every library screen). Only a
                    // problem is said: no Mac paired, or the Mac out of reach.
                    if model.paired == nil {
                        Label("Connect a Mac in Settings or import a file", systemImage: "wifi.slash")
                            .font(.subheadline).foregroundStyle(.secondary)
                    } else if model.macOffline {
                        Label(PudgeModel.unavailable("local audio works"), systemImage: "wifi.slash")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    Picker("Sort", selection: $sortByRecent) {
                        Text("Recently played").tag(true)
                        Text("Title").tag(false)
                    }.pickerStyle(.segmented)
                    if filtered.isEmpty && pending.isEmpty {
                        ContentUnavailableView(filter.isActive ? "Nothing found" : "No audiobooks", systemImage: "books.vertical",
                            description: Text("Download books from the Mac or add audio from the Files app."))
                    }
                    let layout = LibraryLayout(width: width)
                    if !groups.isEmpty {
                        LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                            ForEach(groups) { group in
                                if group.books.count + group.pending.count > 1 {
                                    BookSeriesCard(model: model, group: group, width: layout.card,
                                                   open: { path.append(BookSeriesRoute(title: group.title)) },
                                                   listen: { book in path.append(book) })
                                } else if let book = group.books.first {
                                    BookCard(model: model, book: book, showVolume: false, width: layout.card) { path.append(book) }
                                }
                            }
                        }.libraryGridIdentity(layout)
                    }
                    let onMac = pendingGroups
                    if !onMac.isEmpty {
                        // A lazy catalog: the Mac prepares these (hash, convert) only when asked.
                        LibrarySectionHeader(title: "On the Mac", count: onMac.reduce(0) { $0 + $1.pending.count })
                        LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                            ForEach(onMac) { group in
                                if group.pending.count > 1 {
                                    PendingSeriesCard(model: model, group: group, width: layout.card) {
                                        path.append(BookSeriesRoute(title: group.title))
                                    }
                                } else if let item = group.pending.first {
                                    PendingBookCard(model: model, item: item, width: layout.card)
                                }
                            }
                        }.libraryGridIdentity(layout)
                    }
                }
                .padding(.horizontal, CGFloat(LibraryGrid.padding)).padding(.bottom, 24)
            }
            .libraryWidth($width)
            .background(PudgeTheme.background)
            .navigationTitle("Audiobooks").libraryFilterHeader($filter, prompt: "Book title", device: PudgeTheme.device)
            .navigationDestination(for: AudioManifest.self) { book in BookScreen(model: model, book: book) }
            .navigationDestination(for: BookSeriesRoute.self) { route in
                BookSeriesScreen(model: model, title: route.title) { book in path.append(book) }
            }
            .refreshable { await model.refreshNow() }
            .popsToRootOnReselect($path)
            // No Downloads button (only this screen had one): a book's card and page show its
            // download, and the list shows what is on this device. "+" imports audio files
            // from the Files app (the only way to add a book without the Mac).
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { importing = true } label: { Label("Import audio", systemImage: "plus") }.disabled(model.busy)
                }
                ToolbarItem(placement: .primaryAction) { SettingsButton(model: model) }
            }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.audio], allowsMultipleSelection: true) { result in
                switch result { case .success(let urls): Task { await model.importAudio(urls) }; case .failure(let error): model.message = error.localizedDescription }
            }
        }
    }
}

struct BookSeriesRoute: Hashable { let title: String }

struct BookGroup: Identifiable {
    let title: String
    let books: [AudioManifest]
    /// Volumes of the series still on the Mac (the lazy catalog).
    var pending: [PendingBook] = []
    var id: String { title }
    var volumes: Int { books.count + pending.count }
    /// The series a book is listed under: the Mac's series title, else its own title.
    static func series(_ title: String, _ seriesTitle: String?) -> String {
        seriesTitle?.isEmpty == false ? seriesTitle! : title
    }
    static func ordered(_ items: [PendingBook]) -> [PendingBook] {
        items.sorted { ($0.volume ?? 0, $0.title) < ($1.volume ?? 0, $1.title) }
    }
}

extension PudgeModel {
    /// Downloaded share of a book while its parts are coming (nil when nothing is downloading).
    func downloadFraction(_ book: AudioManifest) -> Double? {
        let records = transfers.filter { $0.assetId == book.assetId }
        guard records.contains(where: { ["queued", "running", "verifying"].contains($0.status) }) else { return nil }
        let received = records.reduce(Int64(0)) { $0 + ($1.status == "complete" ? $1.part.sizeBytes : ($1.receivedBytes ?? 0)) }
        return Double(received) / Double(max(1, book.sizeBytes))
    }
    /// The Mac audiobook card's summary line (its download while this device downloads it).
    func cardStatus(_ book: AudioManifest) -> CardStatus {
        let ready = (try? store.ready(book)) == true
        let position = (try? store.localPosition(assetId: book.assetId, editionId: book.editionId)) ?? 0
        let chapter = book.chapters.last { $0.startMs <= position }?.title
        return CardStatusText.audiobook(positionMs: position, durationMs: book.durationMs, chapter: chapter,
                                        files: book.parts.count, finished: isFinished(book),
                                        ready: ready, downloadFraction: downloadFraction(book))
    }
}

/// An audiobook as a cover card with the Mac audiobook card's summary (place, duration, percent,
/// time left, chapter), and ⋯ with Open and Download / Retry. The card opens the book.
struct BookCard: View {
    @ObservedObject var model: PudgeModel
    let book: AudioManifest
    let showVolume: Bool
    let width: CGFloat
    let open: () -> Void
    var body: some View {
        let ready = (try? model.store.ready(book)) == true
        let downloading = model.downloadFraction(book) != nil
        // Two editions of one book: which one (phone only; the Mac lists one).
        let editions = model.books.filter { $0.assetId == book.assetId }.count
        LibraryCard(title: showVolume && book.volume != nil ? "Volume \(book.volume!)" : book.title,
                    subtitle: editions > 1 ? "Edition " + String(book.editionId.prefix(8)) : nil,
                    status: model.cardStatus(book), badge: ready ? .onDevice(nil) : nil, done: model.isFinished(book), width: width,
                    identifier: "book-card", open: open) {
            BookCardCover(store: model.store, book: book, generation: model.coverGeneration)
        } actions: {
            Button(action: open) { Label("Open", systemImage: "headphones") }
            if let novel = model.novelToFinish(book) {
                Button { Task { await model.markNovelFinished(novel) } } label: {
                    Label("Mark light novel read", systemImage: "book.closed")
                }.disabled(!model.macAvailable)
            }
            if !ready && !downloading {
                Button { model.download(book) } label: {
                    Label(failed ? "Retry download" : "Download to \(PudgeTheme.device)", systemImage: "arrow.down.circle")
                }.disabled(model.paired == nil)
            }
        }
    }
    private var failed: Bool { model.transfers.contains { $0.assetId == book.assetId && ($0.status == "failed" || $0.status == "paused") } }
}

/// Volumes of one series stay together (as on the Mac): one card that opens them.
struct BookSeriesCard: View {
    @ObservedObject var model: PudgeModel
    let group: BookGroup
    let width: CGFloat
    let open: () -> Void
    let listen: (AudioManifest) -> Void
    var body: some View {
        let finished = group.books.filter { model.isFinished($0) }
        let target = group.books.first { !model.isFinished($0) }
        let started = target.map { ((try? model.store.localPosition(assetId: $0.assetId, editionId: $0.editionId)) ?? 0) > 0 } ?? false
        let kept = group.books.filter { (try? model.store.ready($0)) == true }.count
        LibraryCard(title: group.title,
                    status: CardStatusText.audiobookSeries(volumes: group.volumes, unfinished: group.volumes - finished.count),
                    badge: kept > 0 ? .onDevice(kept) : nil, count: group.volumes, width: width,
                    identifier: "book-series-card", open: open) {
            BookCardCover(store: model.store, book: target ?? group.books[0], generation: model.coverGeneration)
        } actions: {
            if let target {
                Button { listen(target) } label: {
                    Label((started ? "Continue " : "Open ") + (target.volume.map { "vol. \($0)" } ?? target.title), systemImage: "headphones")
                }
            }
            Button(action: open) { Label("All \(group.volumes) volumes", systemImage: "square.grid.2x2") }
        }
    }
}

/// A series whose volumes are all still on the Mac: one card (with the first volume's cover)
/// that opens them.
struct PendingSeriesCard: View {
    @ObservedObject var model: PudgeModel
    let group: BookGroup
    let width: CGFloat
    let open: () -> Void
    var body: some View {
        LibraryCard(title: group.title,
                    status: CardStatus("On the Mac", .accent, detail: "\(group.volumes) volumes"),
                    count: group.volumes, width: width, identifier: "pending-series-card", open: open) {
            PendingBookCover(store: model.store, cover: group.pending.first { $0.cover != nil }?.cover,
                             generation: model.coverGeneration)
        } actions: {
            Button(action: open) { Label("All \(group.volumes) volumes", systemImage: "square.grid.2x2") }
        }
    }
}

/// A book in the Mac's lazy catalog: the card asks the Mac to prepare it for download. The volume
/// comes first in its title (a long series title hid it at the end); in its series, only the volume.
struct PendingBookCard: View {
    @ObservedObject var model: PudgeModel
    let item: PendingBook
    var inSeries = false
    let width: CGFloat
    var body: some View {
        let preparing = model.preparingBooks.contains(item.assetId)
        let status = CardStatus(preparing ? "Preparing on the Mac…" : "Prepare on the Mac to download", preparing ? .warn : .accent)
        let title = item.volume.map { inSeries ? "Volume \($0)" : "Vol. \($0) · \(item.seriesTitle ?? item.title)" } ?? item.title
        LibraryCard(title: title,
                    subtitle: item.durationMs.map { PudgeTheme.time($0) }, status: status, width: width,
                    open: preparing || !model.macAvailable ? nil : prepare) {
            PendingBookCover(store: model.store, cover: item.cover, generation: model.coverGeneration)
        }
        .opacity(model.macAvailable ? 1 : 0.6)
    }
    private func prepare() { Task { await model.prepareOnMac(item) } }
}

/// The cover of a book still on the Mac (sent with the catalog), else the download symbol.
struct PendingBookCover: View {
    let store: LocalStore
    let cover: CoverResource?
    var generation = 0
    @State private var image: UIImage?
    var body: some View {
        GeometryReader { proxy in
            Group {
                if let image = image ?? BookCoverImages.cached(cover) {
                    Image(uiImage: image).resizable().scaledToFill()
                } else {
                    ZStack { PudgeTheme.panel; Image(systemName: "arrow.down.circle").font(.system(size: proxy.size.width * 0.3)).foregroundStyle(PudgeTheme.accent) }
                }
            }
            .frame(width: proxy.size.width, height: proxy.size.height).clipped()
        }
        .accessibilityHidden(true)
        .task(id: "\(cover?.resourceId ?? ""):\(cover?.sha256 ?? ""):\(generation)") {
            image = await BookCoverImages.load(cover, store: store)
        }
    }
}

/// The volumes of one audiobook series.
struct BookSeriesScreen: View {
    @ObservedObject var model: PudgeModel
    let title: String
    let open: (AudioManifest) -> Void
    @State private var width: CGFloat = 390
    var books: [AudioManifest] {
        model.books.filter { BookGroup.series($0.title, $0.seriesTitle) == title }
            .sorted { ($0.volume ?? 0, $0.title) < ($1.volume ?? 0, $1.title) }
    }
    /// Its volumes still on the Mac (prepared there on request).
    var pending: [PendingBook] {
        BookGroup.ordered(model.pendingBooks.filter { BookGroup.series($0.title, $0.seriesTitle) == title })
    }
    var body: some View {
        let layout = LibraryLayout(width: width)
        ScrollView {
            LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                ForEach(books) { book in BookCard(model: model, book: book, showVolume: true, width: layout.card) { open(book) } }
                ForEach(pending) { item in PendingBookCard(model: model, item: item, inSeries: true, width: layout.card) }
            }.libraryGridIdentity(layout)
            .padding(.horizontal, CGFloat(LibraryGrid.padding)).padding(.vertical, 12)
        }
        .libraryWidth($width)
        .background(PudgeTheme.background)
        .japaneseNavigationTitle(title)
    }
}

/// Audiobook covers: the store lookup and the file read once per cover, off the main thread
/// (every redraw of a card read both before), scaled down and kept in a cost-limited cache.
enum BookCoverImages {
    /// Covers are shown at most 160 pt wide (× 1.35 high) at 3×.
    static let maxPixels = 800
    private static let cache: NSCache<NSString, UIImage> = {
        let cache = NSCache<NSString, UIImage>(); cache.totalCostLimit = 32 * 1024 * 1024; return cache
    }()
    private static func key(_ cover: CoverResource) -> NSString { (cover.resourceId + ":" + cover.sha256) as NSString }
    static func cached(_ cover: CoverResource?) -> UIImage? { cover.flatMap { cache.object(forKey: key($0)) } }
    static func load(_ cover: CoverResource?, store: LocalStore) async -> UIImage? {
        guard let cover else { return nil }
        if let image = cache.object(forKey: key(cover)) { return image }
        let resourceId = cover.resourceId, sha = cover.sha256
        let loaded = await Task.detached(priority: .utility) { () -> (image: UIImage, current: Bool)? in
            guard let local = try? store.resource(resourceId) else { return nil }
            guard let image = DownsampledImage.load(local.url, maxPixels: BookCoverImages.maxPixels) else { return nil }
            return (image, local.state.sha256 == sha)
        }.value
        guard let loaded else { return nil }
        // The previous picture of a cover changed on the Mac is shown until the new one arrived,
        // but not kept under the new one's key (it stayed until the app ended).
        if loaded.current { cache.setObject(loaded.image, forKey: key(cover), cost: DownsampledImage.cost(loaded.image)) }
        return loaded.image
    }
}

/// An audiobook's cover filling a card (the local cover resource).
struct BookCardCover: View {
    let store: LocalStore
    let book: AudioManifest
    /// PudgeModel.coverGeneration: a cover that arrived later is read then.
    var generation = 0
    @State private var image: UIImage?
    var body: some View {
        GeometryReader { proxy in
            Group {
                if let image = image ?? BookCoverImages.cached(book.cover) {
                    Image(uiImage: image).resizable().scaledToFill()
                } else {
                    ZStack { PudgeTheme.panel; Image(systemName: "headphones").font(.system(size: proxy.size.width * 0.3)).foregroundStyle(PudgeTheme.accent) }
                }
            }
            .frame(width: proxy.size.width, height: proxy.size.height).clipped()
        }
        .accessibilityHidden(true)
        .task(id: "\(book.cover?.resourceId ?? ""):\(book.cover?.sha256 ?? ""):\(generation)") {
            image = await BookCoverImages.load(book.cover, store: store)
        }
    }
}

struct BookCover: View {
    let store: LocalStore
    let book: AudioManifest
    var width: CGFloat = 100
    /// PudgeModel.coverGeneration: a cover that arrived later is read then.
    var generation = 0
    @State private var image: UIImage?
    var body: some View {
        Group {
            if let image = image ?? BookCoverImages.cached(book.cover) {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                ZStack { PudgeTheme.panel; Image(systemName: "headphones").font(.system(size: width * 0.38)).foregroundStyle(PudgeTheme.accent) }
            }
        }.frame(width: width, height: width * 1.35).clipShape(RoundedRectangle(cornerRadius: 10)).accessibilityHidden(true)
        .task(id: "\(book.cover?.resourceId ?? ""):\(book.cover?.sha256 ?? ""):\(generation)") {
            image = await BookCoverImages.load(book.cover, store: store)
        }
    }
}

struct BookScreen: View {
    @ObservedObject var model: PudgeModel
    let book: AudioManifest
    @State private var playerVisible = false
    @State private var offloadConfirmation = false
    @State private var startOverConfirmation = false
    var ready: Bool { (try? model.store.ready(book)) == true }
    var bookTransfers: [TransferRecord] { model.transfers.filter { $0.assetId == book.assetId } }
    var activeTransfers: [TransferRecord] { bookTransfers.filter { ["queued", "running", "verifying"].contains($0.status) } }
    var failedTransfer: TransferRecord? { bookTransfers.first { $0.status == "failed" || $0.status == "paused" } }
    var receivedBytes: Int64 {
        bookTransfers.reduce(Int64(0)) { $0 + ($1.status == "complete" ? $1.part.sizeBytes : ($1.receivedBytes ?? 0)) }
    }
    var pinned: Bool { book.parts.contains { (try? model.store.resource($0.resourceId))?.state.pinned == true } }
    var body: some View {
        List {
            Section {
                HStack { Spacer(); BookCover(store: model.store, book: book, width: 128, generation: model.coverGeneration); Spacer() }
                Text(book.title).font(.title2.bold())
                LabeledContent("Duration", value: PudgeTheme.time(book.durationMs))
                LabeledContent("Size", value: PudgeTheme.size(book.sizeBytes))
                LabeledContent("On \(PudgeTheme.device)", value: ready ? "Available offline" : "Not fully downloaded")
                LabeledContent("Mac", value: model.macLink.status)
                LabeledContent("Position", value: model.syncStatus(book))
                if let verified = model.lastArchive[book.id] { Text("Archive verified " + Date(timeIntervalSince1970: Double(verified)/1000).formatted()).font(.caption).foregroundStyle(.secondary) }
                if model.isFinished(book) {
                    Label("Finished", systemImage: "checkmark.seal.fill").foregroundStyle(PudgeTheme.good)
                        .accessibilityIdentifier("book-finished")
                    // Finished is cleared only here (or on the Mac); Start over keeps it.
                    if model.macSupports("progress-unfinish") {
                        Button("Mark unfinished") { model.markUnfinished(book) }.accessibilityIdentifier("mark-unfinished")
                    }
                    NovelFinishOffer(model: model, media: model.media, book: book)
                }
            }
            Section {
                if ready {
                    Button { Task { await model.continueListening(book); playerVisible = model.player.book != nil } } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Label("Continue", systemImage: "play.fill")
                            if let state = model.progress[book.assetId], let playing = state.playingOn, playing != model.deviceId {
                                Text("Playing on \(state.playingOnName ?? (playing == "mac" ? "the Mac" : "another device")) now — it will pause")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    }
                    .accessibilityIdentifier("play-book").disabled(model.busy)
                    .confirmationDialog("The other device did not confirm the pause",
                                        isPresented: Binding(get: { model.continueFallback?.id == book.id }, set: { if !$0 { model.continueFallback = nil } }),
                                        titleVisibility: .visible) {
                        Button("Continue from saved position") {
                            let saved = model.progress[book.assetId]?.selected
                            model.continueFallback = nil
                            Task { await model.play(book, position: saved?.editionId == book.editionId ? saved?.positionMs : nil); playerVisible = model.player.book != nil }
                        }
                    } message: { Text("It may still be playing.") }
                    Toggle("Keep on \(PudgeTheme.device)", isOn: Binding(get: { pinned }, set: { model.pin(book, value: $0) }))
                    Button { Task { do { try await model.archive(book) } catch { model.message = error.localizedDescription } } } label: { Label("Save archive to Mac", systemImage: "externaldrive.badge.plus") }.disabled(model.paired == nil || model.archiving.contains(book.id))
                    if model.archiving.contains(book.id) { ProgressView("Sending… Keep the app open") }
                    Button("Free up space", role: .destructive) { offloadConfirmation = true }.disabled(pinned)
                    Button("Start over") { startOverConfirmation = true }.accessibilityIdentifier("start-over")
                        .confirmationDialog("Start over from the beginning?", isPresented: $startOverConfirmation, titleVisibility: .visible) {
                            Button("Start over") { Task { await model.play(book, position: 0); model.player.save("reset"); playerVisible = model.player.book != nil } }
                        } message: { Text("Listening starts again at 0:00 on every device. Finished stays as it is.") }
                } else if let active = activeTransfers.first {
                    // Visible feedback right after the tap; details are in «Загрузки».
                    VStack(alignment: .leading, spacing: 6) {
                        Label(active.status == "verifying" ? "Verifying…" : active.status == "queued" ? "Queued…" : "Downloading…",
                              systemImage: "arrow.down.circle")
                        ProgressView(value: Double(receivedBytes), total: Double(max(1, book.sizeBytes)))
                        Text(PudgeTheme.size(receivedBytes) + " / " + PudgeTheme.size(book.sizeBytes)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                } else {
                    if let failed = failedTransfer, let text = failed.message { Text(text).font(.caption).foregroundStyle(.secondary) }
                    Button { model.download(book) } label: {
                        Label(failedTransfer == nil ? "Download to \(PudgeTheme.device)" : "Retry download", systemImage: "arrow.down.circle")
                            .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    }.disabled(model.paired == nil)
                }
            }
            if let state = model.progress[book.assetId], !state.conflicts.isEmpty {
                Section("Choose where to continue") {
                    Text("Devices listened independently. The one you choose applies to every device; all histories are kept.").font(.caption).foregroundStyle(.secondary)
                    ForEach(([state.selected].compactMap { $0 } + state.conflicts).filter { $0.editionId == book.editionId }) { checkpoint in
                        Button {
                            Task { await model.resolve(book, checkpointId: checkpoint.checkpointId) }
                        } label: {
                            VStack(alignment: .leading) {
                                Text(state.deviceLabel(checkpoint.deviceId, this: model.deviceId, thisLabel: "This \(PudgeTheme.device)"))
                                Text(PudgeTheme.time(checkpoint.positionMs) + " · " + Date(timeIntervalSince1970: Double(checkpoint.observedAt)/1000).formatted()).font(.caption)
                            }
                        }
                    }
                }
            }
            Section("Chapters") {
                if book.chapters.isEmpty { Text("No chapter map").foregroundStyle(.secondary) }
                ForEach(book.chapters) { chapter in
                    Button { Task { await model.play(book, position: chapter.startMs); playerVisible = model.player.book != nil } } label: {
                        HStack { Text(chapter.title); Spacer(); Text(PudgeTheme.time(chapter.startMs)).font(.caption.monospacedDigit()) }
                    }.disabled(!ready)
                }
            }
            Section("Bookmarks") {
                ForEach(model.bookmarks[book.id] ?? []) { mark in
                    Button { Task { await model.play(book, position: mark.positionMs); playerVisible = model.player.book != nil } } label: {
                        HStack { Text(mark.title.isEmpty ? "Bookmark" : mark.title); Spacer(); Text(PudgeTheme.time(mark.positionMs)).font(.caption.monospacedDigit()) }
                    }.disabled(!ready).swipeActions { Button("Delete", role: .destructive) { model.deleteBookmark(mark) } }
                }
            }
        }
        .navigationTitle("Audiobook").navigationBarTitleDisplayMode(.inline)
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        // Only books downloaded here are asked on every sync: this one's place and bookmarks now.
        .task { await model.refreshState(book) }
        .task { await model.media.loadNovelListIfNeeded() }
        .sheet(isPresented: $playerVisible) { PlayerScreen(model: model, player: model.player) }
        .confirmationDialog("Delete local audio files?", isPresented: $offloadConfirmation, titleVisibility: .visible) {
            Button("Verify archive and free up space", role: .destructive) { Task { await model.offload(book) } }
        } message: { Text("Pudge verifies an exact copy on the Mac first. Position, chapters and bookmarks stay. Without the Mac's confirmation the files stay on this \(PudgeTheme.device).") }
    }
}

/// A finished audiobook whose linked light novel (Read and listen) is not marked read: one tap
/// marks it read, as the reader's Finish volume; Not now puts the offer away for this book.
struct NovelFinishOffer: View {
    @ObservedObject var model: PudgeModel
    @ObservedObject var media: MediaModel
    let book: AudioManifest
    @State private var busy = false
    var body: some View {
        if let novel = model.novelToFinish(book) {
            VStack(alignment: .leading, spacing: 8) {
                Text("The light novel “\(novel.title)” is not marked read yet.")
                    .font(.footnote).foregroundStyle(.secondary)
                HStack(spacing: 10) {
                    Button {
                        busy = true
                        Task { await model.markNovelFinished(novel); busy = false }
                    } label: {
                        if busy { ProgressView() } else { Label("Mark light novel read", systemImage: "book.closed") }
                    }
                    .buttonStyle(.borderedProminent).disabled(busy || !model.macAvailable)
                    .accessibilityIdentifier("mark-novel-read")
                    Button("Not now") { model.dismissNovelOffer(book) }.buttonStyle(.bordered)
                }
            }
            .padding(.vertical, 2)
        }
    }
}
