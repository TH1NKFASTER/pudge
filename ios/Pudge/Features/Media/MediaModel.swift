import Foundation
import SwiftUI
import UIKit
import Network
import PudgeCore

struct WordRequest: Codable { let wordId: Int; let readingIndex: Int; let surface: String }
struct TextRequest: Codable { let text: String }
struct DeckRequest: Codable { let deckId: String }
struct StudyActionRequest: Codable {
    let action: String
    let grade: String?
    let wordId: Int
    let readingIndex: Int
    let deckId: String
    let sentence: String
    let attemptId: String
}
/// `observedAt` (seconds): when it was read or watched here. A place sent late (after reading
/// offline) never takes a newer place from another iPhone/iPad or the Mac back.
struct NovelPositionRequest: Codable { let chapter: Int; let offset: Double; let finished: Bool?; var observedAt: Double? = Date().timeIntervalSince1970 }
struct MangaPositionRequest: Codable { let page: Int; let read: Bool; var observedAt: Double? = Date().timeIntervalSince1970 }
struct EpisodePositionRequest: Codable { let seconds: Double; let duration: Double; var observedAt: Double? = nil }
struct EmptyRequest: Codable {}
struct OkResponse: Decodable { let ok: Bool? }

/// A word the user tapped, with the sentence it came from.
struct WordLookup: Identifiable, Hashable {
    let id = UUID()
    let token: StudyToken
    let surface: String
    let sentence: String
    var playFromHere: (() -> Void)? = nil
    var playLabel = "Play from here"
    static func == (left: WordLookup, right: WordLookup) -> Bool { left.id == right.id }
    func hash(into hasher: inout Hasher) { hasher.combine(id) }
}

/// Text to split into words on the Mac (manga bubbles, untokenized subtitles).
struct TextLookup: Identifiable { let id = UUID(); let text: String }

/// An episode stored on the iPhone for offline watching.
struct LocalEpisode: Codable, Identifiable, Hashable {
    let id: Int
    let title: String
    let number: Int
    let sha256: String
    let sizeBytes: Int64
    let durationMs: Int64
    var positionMs: Int64
    var positionDirty: Bool
    var savedAt: Double
    var mediaId: Int?
    var subtitleRevision: String?
    /// Seconds actually played on the iPhone (seeks never count).
    var watchedSeconds: IndexSet?
    /// nil: not watched yet; false: waiting to be sent to the Mac; true: counted on the Mac.
    var watchedSent: Bool?
}

/// A light novel or manga volume kept on the iPhone for reading without the Mac
/// (`reading/<kind>-<id>/download.json`); the position read here waits for the Mac.
struct ReadingDownload: Codable, Hashable {
    let kind: String
    let id: Int
    let title: String
    var revision: String
    let sizeBytes: Int64
    let savedAt: Double
    /// Manga: the archive's and the OCR's revisions; `ocr` when the volume has the Mac's complete OCR.
    var pagesRevision: String?
    var ocrRevision: String?
    var ocr: Bool?
    var chapter: Int?
    var offset: Double?
    var page: Int?
    /// Read here since the Mac last confirmed the position; `positionAt`: when (seconds).
    var positionDirty: Bool?
    var positionAt: Double?
    /// Deleted on the Mac (seconds, the Mac's time of deletion): kept readable and marked;
    /// removed a week later unless opened since (`openedAt`).
    var deletedOnMacAt: Double?
    var openedAt: Double?
    var key: String { kind + ":" + String(id) }
}

struct EpisodeWatchedRequest: Codable { let duration: Double; let activeSeconds: Double }

struct GateReviewRequest: Codable { let wordId: Int; let readingIndex: Int; let grade: String; let attemptId: String }

private final class ProgressThrottle: @unchecked Sendable { var last = 0.0 }

/// The Mac answered a file download with an error (its copy is gone or changed).
struct MacRefusedFile: LocalizedError {
    let status: Int
    var errorDescription: String? { "The Mac did not send the file (HTTP \(status))" }
}
private final class BackgroundTaskBox: @unchecked Sendable { var id = UIBackgroundTaskIdentifier.invalid }

struct VideoJob: Equatable {
    var phase: String
    var fraction: Double
}

extension MacConnection {
    func media<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        try await send("/api/v2/media/" + path, as: T.self)
    }
    func media<T: Decodable, V: Encodable>(_ path: String, post value: V, as type: T.Type) async throws -> T {
        try await post("/api/v2/media/" + path, value: value, as: T.self)
    }
}

/// Reading, watching, study and statistics. The Mac is the only sender to Jiten.
@MainActor
final class MediaModel: ObservableObject {
    let app: PudgeModel
    @Published var novels: [LightNovelItem] = []
    @Published var manga: [MangaItem] = []
    @Published var episodes: [EpisodeItem] = []
    /// Anime as the Mac's Anime page offers them (schedule-aware).
    @Published var animeGroups: [AnimeGroup] = []
    /// Episodes waiting for "Download all" (one download at a time).
    @Published var videoQueue: [EpisodeItem] = []
    @Published var localEpisodes: [Int: LocalEpisode] = [:]
    @Published var videoJobs: [Int: VideoJob] = [:]
    /// Light novels and manga volumes on the iPhone ("ln:3", "manga:5").
    @Published var readingDownloads: [String: ReadingDownload] = [:]
    @Published var readingJobs: [String: VideoJob] = [:]
    /// "Get OCR": the Mac's OCR of a manga volume being waited for.
    @Published var ocrJobs: [Int: VideoJob] = [:]
    /// A short note after downloaded volumes were updated in the background.
    @Published var readingNote: String?
    @Published var stats: StatsSummary?
    @Published var planning: PlanningSummary?
    @Published var options: StudyOptions?
    /// Deck chosen on the iPhone (saved on the Mac as the reader's deck).
    @Published var deckOverride: String?
    /// The Mac reader's look (colours, marks, furigana); the last one is kept for offline use.
    @Published var readerLook: ReaderLook? = {
        guard let data = UserDefaults.standard.data(forKey: "pudge.readerLook") else { return nil }
        return try? JSONDecoder().decode(ReaderLook.self, from: data)
    }()
    @Published var loadingReading = false
    @Published var loadingEpisodes = false
    @Published var loadingStats = false
    @Published var readingError: String?
    @Published var episodesError: String?
    @Published var statsError: String?
    @Published var planningError: String?
    private var words: [String: WordDetail] = [:]
    /// Meanings and states from the last updates, for word taps without the Mac.
    static let offlineWordsFile = "offline-words.json"
    private lazy var offlineWords: OfflineWords = OfflineWords.load(offlineWordsURL)
    private var offlineWordsURL: URL { app.store.directory.appendingPathComponent(Self.offlineWordsFile) }
    private var offlineWordsSave: Task<Void, Never>?
    /// Downloaded episodes whose words were checked for missing meanings in this session.
    private var wordsChecked = Set<Int>()
    private var images = NSCache<NSString, UIImage>()
    private var videoTasks: [Int: URLSessionTask] = [:]
    private var videoCancelled = Set<Int>()
    private var readingTasks: [String: URLSessionTask] = [:]
    private var ocrCancelled = Set<Int>()
    private var checkingUpdates = false
    /// Wi-Fi or cellular (updates of downloaded volumes respect "Download over cellular").
    private let network = NWPathMonitor()
    private var readingCancelled = Set<String>()
    /// Downloaded volumes whose words were checked for missing meanings in this session.
    private var readingWordsChecked = Set<String>()
    private var mangaPackages: [Int: MangaPackage] = [:]
    /// Manga text boxes split into words on the Mac (from downloaded volumes).
    private var savedParses: [String: ParsedText]?

    init(app: PudgeModel) {
        self.app = app
        images.countLimit = 60
        loadLocalEpisodes()
        loadReadingDownloads()
        pruneDeletedReading()
        network.start(queue: .main)
    }

    func connection() throws -> MacConnection {
        guard let connection = app.macConnection else { throw PudgeError.invalid("Connect a Mac in Settings") }
        return connection
    }

    // MARK: Library

    static var readingOffline: String { "Mac unavailable · volumes downloaded to the \(PudgeTheme.device) open offline" }
    static let episodesOffline = "Mac unavailable · downloaded episodes play offline"

    /// The Mac answers again: lists, subtitles and saved word data are refreshed.
    func reconnected() async {
        await refreshEpisodes()
        if readingError != nil || !readingDownloads.isEmpty { await refreshReading() }
        if planningError != nil || planning == nil { await refreshPlanning() }
    }

    /// `forceUpdates`: pull to refresh checks downloaded volumes for changes now.
    func refreshReading(forceUpdates: Bool = false) async {
        loadingReading = true; defer { loadingReading = false }
        guard await app.reachMac() else {
            // Without the Mac the lists show the volumes on this iPhone.
            readingError = Self.readingOffline
            novels = savedNovelItems(); manga = savedMangaItems()
            return
        }
        await sendReadingPositions()
        do {
            let client = try connection()
            novels = try await client.media("ln", as: ItemsResponse<LightNovelItem>.self).items
            manga = try await client.media("manga", as: ItemsResponse<MangaItem>.self).items
            // Downloaded volumes the Mac no longer has (deleted there) stay listed.
            novels += savedNovelItems().filter { saved in !novels.contains { $0.id == saved.id } }
            manga += savedMangaItems().filter { saved in !manga.contains { $0.id == saved.id } }
            readingError = nil
            Task { await checkReadingUpdates(force: forceUpdates) }
        } catch {
            readingError = error.localizedDescription
            if novels.isEmpty && manga.isEmpty { novels = savedNovelItems(); manga = savedMangaItems() }
        }
        for record in readingDownloads.values.sorted(by: { $0.savedAt < $1.savedAt }) where !readingWordsChecked.contains(record.key) {
            if await rememberWords(await readingTokens(record)) { readingWordsChecked.insert(record.key) }
        }
    }

    func refreshEpisodes() async {
        loadingEpisodes = true; defer { loadingEpisodes = false }
        // Downloaded episodes stay listed (from the iPhone) and play without the Mac.
        guard await app.reachMac() else { episodesError = Self.episodesOffline; return }
        await sendVideoPositions()
        do {
            animeGroups = try await connection().media("anime", as: ItemsResponse<AnimeGroup>.self).items
            episodes = animeGroups.flatMap(\.episodes)
            episodesError = nil
            await refreshDownloadedSubtitles()
        } catch { episodesError = error.localizedDescription }
    }

    /// Downloaded episodes get the Mac's corrected subtitles (e.g. the right Japanese track).
    private func refreshDownloadedSubtitles() async {
        guard let client = app.macConnection else { return }
        for item in episodes {
            guard var local = localEpisodes[item.id], let revision = item.subtitleRevision, local.subtitleRevision != revision else { continue }
            do {
                let track = try await client.media("episodes/\(item.id)/subtitles", as: SubtitleTrack.self)
                try JSONEncoder().encode(track).write(to: cuesURL(item.id), options: .atomic)
                local.subtitleRevision = revision
                local.mediaId = item.mediaId
                localEpisodes[item.id] = local
                try? JSONEncoder().encode(local).write(to: metaURL(item.id), options: .atomic)
                wordsChecked.remove(item.id)
                await saveWithLiveStates(item.id, track: track)
                AppLog.write("video", "subtitles refreshed for episode \(item.id)")
            } catch { continue }
        }
        for id in localEpisodes.keys.sorted() where !wordsChecked.contains(id) {
            guard let track = subtitles(id) else { continue }
            if await rememberWords(track.cues.flatMap(\.tokens)) { wordsChecked.insert(id) }
        }
    }

    // MARK: Offline word data

    /// Meanings of words in downloaded subtitles, asked from the Mac ahead of time (only
    /// missing ones) so a tap shows them without the Mac. False when the Mac did not answer.
    @discardableResult
    func rememberWords(_ tokens: [StudyToken]) async -> Bool {
        guard let client = app.macConnection else { return false }
        let pairs = offlineWords.missing(tokens)
        guard !pairs.isEmpty else { return true }
        struct Request: Codable { let pairs: [[Int]] }
        var saved = 0
        defer { if saved > 0 { saveOfflineWords(); AppLog.write("study", "\(saved) of \(pairs.count) missing words saved for offline use") } }
        for start in stride(from: 0, to: pairs.count, by: 200) {
            let chunk = Array(pairs[start..<min(pairs.count, start + 200)])
            guard let value = try? await client.media("study/words", post: Request(pairs: chunk), as: ItemsResponse<WordDetail>.self) else { return false }
            offlineWords.remember(value.items)
            saved += value.items.count
        }
        return true
    }

    /// The saved entry of a word (used while the Mac is unavailable).
    func savedWord(_ token: StudyToken) -> WordDetail? { offlineWords.detail(token.key) }
    /// The last known state of a word.
    func savedState(_ token: StudyToken) -> StudyStateRow? { offlineWords.state(token.key) }

    /// Writes the saved word data a moment later (many changes, one write).
    private func saveOfflineWords() {
        offlineWordsSave?.cancel()
        offlineWordsSave = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard let self, !Task.isCancelled else { return }
            let value = self.offlineWords, url = self.offlineWordsURL
            await Task.detached(priority: .utility) {
                do { try value.save(url) } catch { AppLog.write("study", "offline words not saved: \(error.localizedDescription)") }
            }.value
        }
    }

    /// A request failed because the Mac could not be reached (not a refusal or a cancel).
    private func unreachable(_ error: Error) -> Bool {
        guard let failure = error as? URLError else { return false }
        return failure.code != .cancelled
    }

    /// The user's JitenMPV subtitle look (kept for offline playback).
    @Published var subtitleLook: SubtitleLook? = {
        guard let data = UserDefaults.standard.data(forKey: "pudge.subtitleLook") else { return nil }
        return try? JSONDecoder().decode(SubtitleLook.self, from: data)
    }()

    func loadSubtitleLook() async {
        guard !app.macOffline, let look = try? await connection().media("subtitle-look", as: SubtitleLook.self) else { return }
        subtitleLook = look
        if let data = try? JSONEncoder().encode(look) { UserDefaults.standard.set(data, forKey: "pudge.subtitleLook") }
    }

    /// Asked on every open of a reader (the Mac's colours may have changed); the saved look shows meanwhile.
    func loadReaderLook() async {
        guard !app.macOffline, let look = try? await connection().media("reader", as: ReaderLook.self) else { return }
        guard look != readerLook else { return }
        readerLook = look
        if let data = try? JSONEncoder().encode(look) { UserDefaults.standard.set(data, forKey: "pudge.readerLook") }
    }

    // MARK: Review gate (as on the Mac before an episode)

    func gateBegin(_ episode: Int) async throws -> GateStatus {
        try await connection().media("episodes/\(episode)/gate/begin", post: EmptyRequest(), as: GateStatus.self)
    }

    func gateReview(_ episode: Int, card: GateCard, grade: String) async -> Result<GateStatus, Error> {
        do {
            let request = GateReviewRequest(wordId: card.wordId, readingIndex: card.readingIndex, grade: grade,
                                            attemptId: UUID().uuidString.lowercased())
            return .success(try await connection().media("episodes/\(episode)/gate/review", post: request, as: GateStatus.self))
        } catch { return .failure(error) }
    }

    func refreshStats(period: String) async {
        loadingStats = true; defer { loadingStats = false }
        guard await app.reachMac() else { statsError = "Mac unavailable. Statistics come from the Mac."; return }
        do {
            let client = try connection()
            stats = try await client.media("stats?period=" + period, as: StatsSummary.self)
            statsError = nil
        } catch { statsError = error.localizedDescription }
    }

    func refreshPlanning() async {
        guard await app.reachMac() else { planningError = Self.episodesOffline; return }
        do {
            planning = try await connection().media("planning", as: PlanningSummary.self)
            planningError = nil
        } catch { planningError = error.localizedDescription }
    }

    func image(_ path: String) async -> UIImage? {
        if let cached = images.object(forKey: path as NSString) { return cached }
        if let local = localReadingImage(path), let image = UIImage(contentsOfFile: local.path) {
            images.setObject(image, forKey: path as NSString); return image
        }
        let folder = app.store.directory.appendingPathComponent("image-cache", isDirectory: true)
        let file = folder.appendingPathComponent(path.replacingOccurrences(of: "/", with: "_"))
        // Covers and illustrations (content-addressed on the Mac) are kept on disk.
        let keep = path.hasPrefix("cover/") || path.hasPrefix("ln-image/")
        if keep, let image = UIImage(contentsOfFile: file.path) {
            images.setObject(image, forKey: path as NSString); return image
        }
        guard let data = try? await connection().bytes("/api/v2/media/" + path), let image = UIImage(data: data) else { return nil }
        images.setObject(image, forKey: path as NSString)
        if keep {
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try? data.write(to: file, options: .atomic)
        }
        return image
    }

    // MARK: Light novels

    func novel(_ id: Int) async throws -> LightNovelDetail {
        // A downloaded volume opens from the iPhone when the Mac does not answer.
        if let saved = savedNovel(id)?.book, !(await app.reachMac()) { return saved }
        do { return try await connection().media("ln/\(id)", as: LightNovelDetail.self) }
        catch { if let saved = savedNovel(id)?.book { return saved }; throw error }
    }

    /// Where a volume opens: the place read on this iPhone while the Mac did not know it yet.
    func startPosition(_ id: Int, _ detail: LightNovelDetail) -> (chapter: Int, offset: Double) {
        if let record = readingDownloads["ln:\(id)"], let chapter = record.chapter, record.positionDirty == true || app.macOffline {
            return (chapter, record.offset ?? 0)
        }
        return (detail.chapter, detail.offset)
    }

    /// Chapter text; while the Mac is still parsing words, polls until tokens arrive.
    func chapter(_ book: Int, index: Int, update: (ChapterText) -> Void) async throws {
        // Downloaded chapters open from the iPhone (fresh word colours follow when the Mac answers).
        if var saved = savedChapter(book, index: index) {
            // Words graded since the download keep the colours of the last update.
            let newer = offlineWords.states(newerThan: readingDownloads["ln:\(book)"]?.savedAt ?? 0)
            if !newer.isEmpty { for row in saved.paragraphs.indices { saved.paragraphs[row].tokens = saved.paragraphs[row].tokens.updating(newer) } }
            update(saved)
            if app.macOffline || saved.paragraphs.contains(where: { !$0.tokens.isEmpty }) { return }
        }
        let client = try connection()
        var value = try await client.media("ln/\(book)/chapters/\(index)", as: ChapterText.self)
        update(value)
        var attempts = 0
        while value.parsing && attempts < 90 && !Task.isCancelled {
            attempts += 1
            try await Task.sleep(nanoseconds: 2_000_000_000)
            let next = try await client.media("ln/\(book)/chapters/\(index)?poll=1", as: ChapterText.self)
            if !next.parsing { value = next; update(next) }
        }
    }

    func saveNovelPosition(_ book: Int, chapter: Int, offset: Double, finished: Bool? = nil) async {
        let key = "ln:\(book)"
        if var record = readingDownloads[key] {
            record.chapter = chapter; record.offset = offset; record.positionDirty = true; record.positionAt = Date().timeIntervalSince1970
            saveReadingRecord(record)
        }
        guard !app.macOffline else { return }
        guard (try? await connection().media("ln/\(book)/position",
            post: NovelPositionRequest(chapter: chapter, offset: offset, finished: finished), as: OkResponse.self)) != nil else { return }
        if var record = readingDownloads[key], record.chapter == chapter, record.offset == offset {
            record.positionDirty = false; saveReadingRecord(record)
        }
    }

    // MARK: Manga

    func mangaText(_ book: Int, page: Int) async -> MangaPageText? {
        if readingDownloads["manga:\(book)"] != nil {
            let file = readingURL("manga", book).appendingPathComponent("text-\(page).json")
            if let data = try? Data(contentsOf: file), let value = try? JSONDecoder().decode(MangaPageText.self, from: data) { return value }
        }
        return try? await connection().media("manga/\(book)/pages/\(page)/text", as: MangaPageText.self)
    }

    /// The page a volume opens on (read further on this iPhone than the Mac knows).
    func startPage(_ book: MangaItem) -> Int {
        if let record = readingDownloads["manga:\(book.id)"], let page = record.page, record.positionDirty == true || app.macOffline { return page }
        return book.position
    }

    func saveMangaPosition(_ book: Int, page: Int) async {
        let key = "manga:\(book)"
        if var record = readingDownloads[key] {
            record.page = page; record.positionDirty = true; record.positionAt = Date().timeIntervalSince1970
            saveReadingRecord(record)
        }
        guard !app.macOffline else { return }
        guard (try? await connection().media("manga/\(book)/position", post: MangaPositionRequest(page: page, read: true), as: OkResponse.self)) != nil else { return }
        if var record = readingDownloads[key], record.page == page { record.positionDirty = false; saveReadingRecord(record) }
    }

    // MARK: Reading on iPhone (downloaded light novels and manga)

    var readingFolder: URL { app.store.directory.appendingPathComponent("reading", isDirectory: true) }
    func readingURL(_ kind: String, _ id: Int) -> URL { readingFolder.appendingPathComponent("\(kind)-\(id)", isDirectory: true) }
    func isDownloaded(_ kind: String, _ id: Int) -> Bool { readingDownloads["\(kind):\(id)"] != nil }
    var readingBytes: Int64 { readingDownloads.values.reduce(0) { $0 + $1.sizeBytes } }
    /// Light novels the user removed: not downloaded again with their audiobook.
    private var readingRemoved: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: "pudge.readingRemoved") ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: "pudge.readingRemoved") }
    }

    func loadReadingDownloads() {
        var result: [String: ReadingDownload] = [:]
        let manager = FileManager.default
        let folders = (try? manager.contentsOfDirectory(at: readingFolder, includingPropertiesForKeys: nil)) ?? []
        for folder in folders {
            if folder.pathExtension == "part" && readingJobs.isEmpty { try? manager.removeItem(at: folder); continue }  // interrupted
            if folder.lastPathComponent.hasSuffix(".unpacking") { try? manager.removeItem(at: folder); continue }
            guard let data = try? Data(contentsOf: folder.appendingPathComponent("download.json")),
                  let record = try? JSONDecoder().decode(ReadingDownload.self, from: data) else { continue }
            result[record.key] = record
        }
        readingDownloads = result
        mangaPackages = [:]; savedParses = nil
    }

    private func saveReadingRecord(_ record: ReadingDownload) {
        readingDownloads[record.key] = record
        try? JSONEncoder().encode(record).write(to: readingURL(record.kind, record.id).appendingPathComponent("download.json"), options: .atomic)
    }

    func savedNovel(_ id: Int) -> NovelPackage? {
        guard isDownloaded("ln", id), let data = try? Data(contentsOf: readingURL("ln", id).appendingPathComponent("package.json")) else { return nil }
        return try? JSONDecoder().decode(NovelPackage.self, from: data)
    }

    func savedChapter(_ book: Int, index: Int) -> ChapterText? {
        guard isDownloaded("ln", book),
              let data = try? Data(contentsOf: readingURL("ln", book).appendingPathComponent("chapter-\(index).json")) else { return nil }
        return try? JSONDecoder().decode(ChapterText.self, from: data)
    }

    func savedManga(_ id: Int) -> MangaPackage? {
        if let cached = mangaPackages[id] { return cached }
        guard isDownloaded("manga", id), let data = try? Data(contentsOf: readingURL("manga", id).appendingPathComponent("package.json")),
              let value = try? JSONDecoder().decode(MangaPackage.self, from: data) else { return nil }
        mangaPackages[id] = value
        return value
    }

    /// Library rows of the downloaded volumes (the lists without the Mac).
    func savedNovelItems() -> [LightNovelItem] {
        readingDownloads.values.filter { $0.kind == "ln" }.compactMap { savedNovel($0.id)?.item }.sorted { $0.updatedAt > $1.updatedAt }
    }
    func savedMangaItems() -> [MangaItem] {
        readingDownloads.values.filter { $0.kind == "manga" }.compactMap { savedManga($0.id)?.item }.sorted { $0.updatedAt > $1.updatedAt }
    }

    /// Covers, illustrations and manga pages of downloaded volumes.
    private func localReadingImage(_ path: String) -> URL? {
        let parts = path.split(separator: "/").map(String.init)
        var candidates: [URL] = []
        if parts.count == 2, parts[0] == "ln-image", ReadingPackage.isSafe(parts[1]) {
            candidates = readingDownloads.values.filter { $0.kind == "ln" }.map { readingURL("ln", $0.id).appendingPathComponent("image-" + parts[1]) }
        } else if parts.count == 3, parts[0] == "cover", ["ln", "manga"].contains(parts[1]), let id = Int(parts[2]), isDownloaded(parts[1], id) {
            candidates = [readingURL(parts[1], id).appendingPathComponent("cover.jpg")]
        } else if parts.count == 4, parts[0] == "manga", parts[2] == "pages", let id = Int(parts[1]), let page = Int(parts[3]),
                  let name = savedManga(id)?.pages[safe: page], ReadingPackage.isSafe(name) {
            candidates = [readingURL("manga", id).appendingPathComponent(name)]
        }
        return candidates.first { FileManager.default.fileExists(atPath: $0.path) }
    }

    private func savedParse(_ text: String) -> ParsedText? {
        if savedParses == nil {
            var all: [String: ParsedText] = [:]
            for record in readingDownloads.values where record.kind == "manga" {
                guard let data = try? Data(contentsOf: readingURL("manga", record.id).appendingPathComponent("parsed.json")),
                      let value = try? JSONDecoder().decode(ParsedRegions.self, from: data) else { continue }
                for item in value.items { all[item.text] = ParsedText(paragraphs: item.paragraphs) }
            }
            savedParses = all
        }
        return savedParses?[text]
    }

    /// Words of a downloaded volume (their meanings are kept for word cards without the Mac).
    private func readingTokens(_ record: ReadingDownload) async -> [StudyToken] {
        let folder = readingURL(record.kind, record.id)
        let kind = record.kind
        return await Task.detached(priority: .utility) {
            let files = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
            var tokens: [StudyToken] = []
            for name in files.sorted() {
                guard let data = try? Data(contentsOf: folder.appendingPathComponent(name)) else { continue }
                if kind == "ln", name.hasPrefix("chapter-"), let chapter = try? JSONDecoder().decode(ChapterText.self, from: data) {
                    tokens += chapter.paragraphs.flatMap(\.tokens)
                } else if kind == "manga", name == "parsed.json", let parsed = try? JSONDecoder().decode(ParsedRegions.self, from: data) {
                    tokens += parsed.items.flatMap { $0.paragraphs.flatMap(\.tokens) }
                }
            }
            return tokens
        }.value
    }

    /// The light novel of a downloaded audiobook comes along (read-along offline), unless removed.
    func downloadLinkedNovel(_ id: Int, title: String) {
        guard !isDownloaded("ln", id), readingJobs["ln:\(id)"] == nil, !readingRemoved.contains("ln:\(id)"), app.macConnection != nil else { return }
        AppLog.write("reading", "light novel \(id) follows its audiobook")
        Task { await downloadReading("ln", id: id, title: title, quiet: true) }
    }

    func cancelReading(_ kind: String, _ id: Int) {
        let key = "\(kind):\(id)"
        readingCancelled.insert(key)
        readingTasks[key]?.cancel()
        readingTasks[key] = nil
        readingJobs[key] = nil
    }

    func deleteReading(_ kind: String, _ id: Int) {
        cancelReading(kind, id)
        try? FileManager.default.removeItem(at: readingURL(kind, id))
        readingRemoved.insert("\(kind):\(id)")
        loadReadingDownloads()
        AppLog.write("reading", "\(kind) \(id) removed from this device")
    }

    /// The Mac packs the volume (chapters with words, illustrations; manga pages and text
    /// boxes); one verified file comes over and is unpacked here.
    @discardableResult
    func downloadReading(_ kind: String, id: Int, title: String, quiet: Bool = false) async -> Bool {
        let key = "\(kind):\(id)"
        guard readingJobs[key] == nil else { return false }
        // An update keeps the place read on this iPhone.
        let previous = readingDownloads[key]
        readingCancelled.remove(key)
        var removed = readingRemoved; removed.remove(key); readingRemoved = removed
        readingJobs[key] = VideoJob(phase: "Preparing on the Mac…", fraction: 0)
        let partial = readingFolder.appendingPathComponent("\(kind)-\(id).part")
        defer { readingJobs[key] = nil; readingTasks[key] = nil }
        func checkCancelled() throws { if readingCancelled.contains(key) { throw CancellationError() } }
        do {
            let client = try connection()
            let route = "\(kind)/\(id)/package"
            var state = try await client.media(route, post: EmptyRequest(), as: ReadingPackageState.self)
            while state.state == "preparing" || state.state == "idle" {
                try checkCancelled()
                readingJobs[key] = VideoJob(phase: "Preparing on the Mac…", fraction: state.progress)
                try await Task.sleep(nanoseconds: 1_500_000_000)
                if state.state == "idle" {  // the Mac restarted: ask again
                    state = try await client.media(route, post: EmptyRequest(), as: ReadingPackageState.self)
                } else {
                    state = try await client.media(route, as: ReadingPackageState.self)
                }
            }
            guard state.state == "ready", let package = state.package else {
                throw PudgeError.invalid(state.message.isEmpty ? "The Mac could not prepare this volume" : state.message)
            }
            try FileManager.default.createDirectory(at: readingFolder, withIntermediateDirectories: true)
            var folder = readingFolder
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            try? folder.setResourceValues(values)
            let free = (try? readingFolder.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage) ?? Int64.max
            guard Int64(free) > package.sizeBytes * 2 + 300_000_000 else { throw PudgeError.invalid("Not enough free space on this \(PudgeTheme.device)") }
            try checkCancelled()
            readingJobs[key] = VideoJob(phase: "Downloading…", fraction: 0)
            try await transfer("/api/v2/resources/\(package.resourceId)/content?sha256=\(package.sha256)", client: client, to: partial,
                               started: { [weak self] task in self?.readingTasks[key] = task }) { [weak self] value in
                if self?.readingJobs[key] != nil { self?.readingJobs[key] = VideoJob(phase: "Downloading…", fraction: value) }
            }
            try checkCancelled()
            readingJobs[key] = VideoJob(phase: "Verifying…", fraction: 1)
            let target = readingURL(kind, id), sha = package.sha256, size = package.sizeBytes
            try await Task.detached(priority: .utility) {
                try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size)
                try ReadingPackage.unpack(partial, into: target)
            }.value
            try? FileManager.default.removeItem(at: partial)
            struct Described: Decodable { let title: String }
            let described = (try? Data(contentsOf: target.appendingPathComponent("package.json"))).flatMap { try? JSONDecoder().decode(Described.self, from: $0) }
            let manga = kind == "manga" ? (try? Data(contentsOf: target.appendingPathComponent("package.json"))).flatMap { try? JSONDecoder().decode(MangaPackage.self, from: $0) } : nil
            var record = ReadingDownload(kind: kind, id: id, title: described?.title ?? title, revision: state.revision,
                                         sizeBytes: size, savedAt: Date().timeIntervalSince1970,
                                         pagesRevision: manga?.pagesRevision ?? state.pagesRevision,
                                         ocrRevision: manga?.ocr == true ? manga?.ocrRevision : nil, ocr: manga.map { $0.ocr == true })
            record.chapter = previous?.chapter; record.offset = previous?.offset; record.page = previous?.page
            record.positionDirty = previous?.positionDirty
            record.positionAt = previous?.positionAt; record.openedAt = previous?.openedAt
            try JSONEncoder().encode(record).write(to: target.appendingPathComponent("download.json"), options: .atomic)
            loadReadingDownloads()
            AppLog.write("reading", "\(previous == nil ? "downloaded" : "updated") \(kind) \(id) \(size) bytes")
            readingWordsChecked.remove(key)
            Task {
                if await rememberWords(await readingTokens(record)) { readingWordsChecked.insert(key) }
            }
            return true
        } catch {
            try? FileManager.default.removeItem(at: partial)
            AppLog.write("reading", "\(kind) \(id) not downloaded: \(error.localizedDescription)")
            if !readingCancelled.contains(key) && !quiet { app.message = error.localizedDescription }
            return false
        }
    }

    // MARK: Manga OCR on request ("Get OCR")

    /// The volume has no complete OCR on the iPhone (downloaded) or on the Mac (listed).
    func needsOCR(_ book: MangaItem) -> Bool {
        if let record = readingDownloads["manga:\(book.id)"] { return record.ocr != true }
        return book.pages > 0 && book.ocrPages < book.pages
    }

    func cancelOCR(_ id: Int) { ocrCancelled.insert(id); ocrJobs[id] = nil }

    /// Asks the Mac to OCR the whole volume (Mokuro), follows it, and when the Mac reports the
    /// volume complete the OCR comes to a downloaded volume in one go (never part of it).
    func getOCR(_ id: Int, title: String) async {
        guard ocrJobs[id] == nil else { return }
        guard app.macConnection != nil, !app.macOffline, await app.reachMac() else {
            app.message = "Getting OCR needs the Mac: the Mac runs the OCR. Try again when the \(PudgeTheme.device) reaches it."; return
        }
        ocrCancelled.remove(id)
        ocrJobs[id] = VideoJob(phase: "Starting OCR on the Mac…", fraction: 0)
        defer { ocrJobs[id] = nil }
        do {
            let client = try connection()
            var status = try await client.media("manga/\(id)/ocr", post: EmptyRequest(), as: MangaOCRStatus.self)
            AppLog.write("reading", "OCR of manga \(id) asked: \(status.state) \(status.completedPages)/\(status.totalPages)")
            var idle = 0
            while !status.complete {
                if ocrCancelled.contains(id) { return }
                if status.running { idle = 0 } else { idle += 1 }
                // Stopped without finishing (failed pages, paused, Mac restarted).
                if idle >= 3 {
                    throw PudgeError.invalid(status.message.isEmpty
                        ? "The Mac stopped the OCR at \(status.completedPages) of \(status.totalPages) pages. Try again or check the volume on the Mac."
                        : status.message)
                }
                ocrJobs[id] = VideoJob(phase: "OCR on the Mac: \(status.completedPages) of \(status.totalPages) pages", fraction: status.progress)
                try await Task.sleep(nanoseconds: 4_000_000_000)
                status = try await client.media("manga/\(id)/ocr", as: MangaOCRStatus.self)
            }
            AppLog.write("reading", "OCR of manga \(id) complete on the Mac")
            if isDownloaded("manga", id) { try await downloadOCR(id, client: client) }
            else { readingNote = "OCR of “\(title)” is ready on the Mac" }
        } catch is CancellationError {
        } catch {
            AppLog.write("reading", "OCR of manga \(id): \(error.localizedDescription)")
            if !ocrCancelled.contains(id) { app.message = error.localizedDescription }
        }
    }

    /// The finished OCR of a downloaded volume: text boxes and their words, without the pages again.
    private func downloadOCR(_ id: Int, client: MacConnection) async throws {
        let route = "manga/\(id)/ocr-package"
        ocrJobs[id] = VideoJob(phase: "Preparing the OCR on the Mac…", fraction: 0)
        var state = try await client.media(route, post: EmptyRequest(), as: ReadingPackageState.self)
        while state.state == "preparing" || state.state == "idle" {
            if ocrCancelled.contains(id) { throw CancellationError() }
            try await Task.sleep(nanoseconds: 1_500_000_000)
            if state.state == "idle" { state = try await client.media(route, post: EmptyRequest(), as: ReadingPackageState.self) }
            else { state = try await client.media(route, as: ReadingPackageState.self) }
        }
        guard state.state == "ready", let package = state.package else {
            throw PudgeError.invalid(state.message.isEmpty ? "The Mac could not prepare the OCR" : state.message)
        }
        let partial = readingFolder.appendingPathComponent("manga-\(id).ocr.part")
        let unpacked = readingFolder.appendingPathComponent("manga-\(id).ocr", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: partial); try? FileManager.default.removeItem(at: unpacked) }
        ocrJobs[id] = VideoJob(phase: "Downloading the OCR…", fraction: 0)
        try await transfer("/api/v2/resources/\(package.resourceId)/content?sha256=\(package.sha256)", client: client, to: partial,
                           started: { _ in }) { [weak self] value in
            if self?.ocrJobs[id] != nil { self?.ocrJobs[id] = VideoJob(phase: "Downloading the OCR…", fraction: value) }
        }
        let sha = package.sha256, size = package.sizeBytes, folder = readingURL("manga", id)
        try await Task.detached(priority: .utility) {
            try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size)
            let names = try ReadingPackage.unpack(partial, into: unpacked)
            // Text boxes and words move into the volume; its pages stay.
            for name in names where name.hasPrefix("text-") || name == "parsed.json" {
                let target = folder.appendingPathComponent(name)
                try? FileManager.default.removeItem(at: target)
                try FileManager.default.moveItem(at: unpacked.appendingPathComponent(name), to: target)
            }
        }.value
        guard var record = readingDownloads["manga:\(id)"] else { return }
        record.ocr = true; record.ocrRevision = state.revision
        saveReadingRecord(record)
        savedParses = nil
        readingWordsChecked.remove(record.key)
        readingNote = "OCR added to “\(record.title)”"
        AppLog.write("reading", "OCR of manga \(id) downloaded \(size) bytes")
        Task { if await rememberWords(await readingTokens(record)) { readingWordsChecked.insert(record.key) } }
    }

    // MARK: Updates of downloaded volumes

    /// Now and then (every 6 hours, or when asked) downloaded volumes are compared with the Mac
    /// and updated in the background: changed text or pages, or OCR finished on the Mac. Only
    /// when the Mac answers, and over cellular only with "Download over cellular".
    func checkReadingUpdates(force: Bool = false) async {
        guard !readingDownloads.isEmpty, !checkingUpdates, let client = app.macConnection, !app.macOffline else { return }
        let last = UserDefaults.standard.double(forKey: "pudge.readingCheckedAt")
        guard force || Date().timeIntervalSince1970 - last >= 6 * 3600 else { return }
        let path = network.currentPath
        if path.usesInterfaceType(.cellular) && !path.usesInterfaceType(.wifi) && !UserDefaults.standard.bool(forKey: "pudge.cellularDownloads") { return }
        checkingUpdates = true; defer { checkingUpdates = false }
        struct Item: Codable { let kind: String; let id: Int }
        struct Request: Codable { let items: [Item] }
        let records = readingDownloads.values.sorted { $0.savedAt < $1.savedAt }
        guard let answer = try? await client.media("downloads/check", post: Request(items: records.map { Item(kind: $0.kind, id: $0.id) }),
                                                   as: ReadingRevisions.self) else { return }
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "pudge.readingCheckedAt")
        var updated: [String] = []
        for row in answer.items {
            guard var record = readingDownloads["\(row.kind):\(row.id)"], readingJobs[record.key] == nil else { continue }
            // Deleted on the Mac: marked and kept readable (removed a week later unless opened).
            if row.missing == true {
                if record.deletedOnMacAt == nil {
                    record.deletedOnMacAt = row.deletedAt ?? Date().timeIntervalSince1970
                    saveReadingRecord(record)
                    AppLog.write("reading", "\(record.kind) \(record.id) deleted on the Mac: kept, marked")
                }
                continue
            } else if record.deletedOnMacAt != nil {
                record.deletedOnMacAt = nil; saveReadingRecord(record)
            }
            switch ReadingRevisions.update(row, revision: record.revision, pagesRevision: record.pagesRevision, ocrRevision: record.ocrRevision) {
            case .none: continue
            case .ocr:
                guard ocrJobs[row.id] == nil else { continue }
                ocrJobs[row.id] = VideoJob(phase: "Updating the OCR…", fraction: 0)
                do { try await downloadOCR(row.id, client: client); updated.append(record.title) }
                catch { AppLog.write("reading", "OCR update of manga \(row.id) failed: \(error.localizedDescription)") }
                ocrJobs[row.id] = nil
            case .volume:
                AppLog.write("reading", "\(row.kind) \(row.id) changed on the Mac: updating")
                if await downloadReading(row.kind, id: row.id, title: record.title, quiet: true) { updated.append(record.title) }
            }
        }
        pruneDeletedReading()
        if !updated.isEmpty {
            readingNote = (updated.count == 1 ? "Updated on \(PudgeTheme.device): " : "Updated on \(PudgeTheme.device) (\(updated.count)): ") + updated.joined(separator: ", ")
        }
    }

    /// Opening a volume deleted on the Mac keeps it until the user removes it.
    func markOpened(_ kind: String, _ id: Int) {
        guard var record = readingDownloads["\(kind):\(id)"] else { return }
        record.openedAt = Date().timeIntervalSince1970
        saveReadingRecord(record)
    }

    /// Volumes deleted on the Mac a week ago and not opened since leave this device.
    func pruneDeletedReading(now: Double = Date().timeIntervalSince1970) {
        let expired = readingDownloads.values.filter { ReadingRevisions.removable(deletedAt: $0.deletedOnMacAt, openedAt: $0.openedAt, now: now) }
        for record in expired {
            deleteReading(record.kind, record.id)
            AppLog.write("reading", "\(record.kind) \(record.id) removed: deleted on the Mac a week ago, not opened since")
        }
        if let first = expired.first {
            readingNote = "Removed “\(first.title)”\(expired.count > 1 ? " and \(expired.count - 1) more" : ""): deleted on the Mac a week ago"
        }
    }

    /// Places read on this iPhone without the Mac go to the Mac once it answers.
    func sendReadingPositions() async {
        guard let client = app.macConnection, !app.macOffline else { return }
        for record in readingDownloads.values where record.positionDirty == true {
            do {
                if record.kind == "ln", let chapter = record.chapter {
                    _ = try await client.media("ln/\(record.id)/position",
                        post: NovelPositionRequest(chapter: chapter, offset: record.offset ?? 0, finished: nil, observedAt: record.positionAt), as: OkResponse.self)
                } else if record.kind == "manga", let page = record.page {
                    _ = try await client.media("manga/\(record.id)/position", post: MangaPositionRequest(page: page, read: true, observedAt: record.positionAt), as: OkResponse.self)
                }
                if var sent = readingDownloads[record.key], sent.chapter == record.chapter, sent.offset == record.offset, sent.page == record.page {
                    sent.positionDirty = false; saveReadingRecord(sent)
                }
            } catch { return }
        }
    }

    // MARK: Study

    func loadOptions() async {
        if options != nil || app.macOffline { return }
        options = try? await connection().media("study/options", as: StudyOptions.self)
    }

    func word(_ token: StudyToken, surface: String, refresh: Bool = false) async throws -> WordDetail {
        let key = "\(token.wordId):\(token.readingIndex)"
        if !refresh, let cached = words[key], cached.complete { return cached }
        do {
            let value = try await connection().media("study/word",
                post: WordRequest(wordId: token.wordId, readingIndex: token.readingIndex, surface: surface), as: WordDetail.self)
            words[key] = value
            if value.complete { offlineWords.remember([value]); saveOfflineWords() }
            return value
        } catch {
            if unreachable(error) { app.macAvailable = false }
            throw error
        }
    }

    func parse(_ text: String) async throws -> ParsedText {
        if app.macOffline {
            if let saved = savedParse(text) { return saved }
            throw PudgeError.invalid("Splitting text into words needs the Mac. Words of downloaded subtitles and manga volumes still open.")
        }
        do { return try await connection().media("study/parse", post: TextRequest(text: text), as: ParsedText.self) }
        catch { if let saved = savedParse(text) { return saved }; throw error }
    }

    func saveDeck(_ deck: String) async {
        _ = try? await connection().media("study/deck", post: DeckRequest(deckId: deck), as: OkResponse.self)
        deckOverride = deck
    }

    /// Add or review through the Mac. One attempt id per tap: an unknown outcome is
    /// reported and never retried automatically.
    func study(_ action: String, grade: String? = nil, token: StudyToken, sentence: String, deck: String) async -> StudyActionResult {
        let request = StudyActionRequest(action: action, grade: grade, wordId: token.wordId, readingIndex: token.readingIndex,
                                         deckId: deck, sentence: sentence, attemptId: UUID().uuidString.lowercased())
        do {
            let result = try await connection().media("study/action", post: request, as: StudyActionResult.self)
            words["\(token.wordId):\(token.readingIndex)"] = nil
            AppLog.write("study", "\(action) \(grade ?? "") \(token.wordId): \(result.outcome)")
            return result
        } catch {
            AppLog.write("study", "\(action) \(token.wordId): no answer")
            return StudyActionResult(ok: false, outcome: "unknown",
                message: "No answer from the Mac. The word may or may not be saved — check before trying again.")
        }
    }

    // MARK: Video on iPhone

    var videoFolder: URL { app.store.directory.appendingPathComponent("video", isDirectory: true) }
    func videoURL(_ id: Int) -> URL { videoFolder.appendingPathComponent("\(id).mp4") }
    private func cuesURL(_ id: Int) -> URL { videoFolder.appendingPathComponent("\(id).cues.json") }
    private func metaURL(_ id: Int) -> URL { videoFolder.appendingPathComponent("\(id).json") }

    func loadLocalEpisodes() {
        var result: [Int: LocalEpisode] = [:]
        let files = (try? FileManager.default.contentsOfDirectory(at: videoFolder, includingPropertiesForKeys: nil)) ?? []
        for file in files where file.pathExtension == "part" && videoJobs.isEmpty {
            try? FileManager.default.removeItem(at: file)  // interrupted download (app was closed)
        }
        for file in files where file.pathExtension == "json" && !file.lastPathComponent.hasSuffix(".cues.json") {
            guard let data = try? Data(contentsOf: file), let item = try? JSONDecoder().decode(LocalEpisode.self, from: data),
                  FileManager.default.fileExists(atPath: videoURL(item.id).path) else { continue }
            result[item.id] = item
        }
        localEpisodes = result
    }

    /// Downloaded subtitles; words take the states saved after the file was written
    /// (e.g. graded later), so colours stay as fresh as the last update.
    func subtitles(_ id: Int) -> SubtitleTrack? {
        guard let data = try? Data(contentsOf: cuesURL(id)), var track = try? JSONDecoder().decode(SubtitleTrack.self, from: data) else { return nil }
        let written = ((try? cuesURL(id).resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast).timeIntervalSince1970
        let newer = offlineWords.states(newerThan: written)
        if !newer.isEmpty {
            for index in track.cues.indices { track.cues[index].tokens = track.cues[index].tokens.updating(newer) }
        }
        return track
    }

    /// Keeps the live states of a downloaded episode's words with its subtitles (offline colours).
    func saveSubtitles(_ id: Int, track: SubtitleTrack) {
        guard localEpisodes[id] != nil else { return }
        try? JSONEncoder().encode(track).write(to: cuesURL(id), options: .atomic)
    }

    /// Fresh subtitles from the Mac get the current Jiten states before they are kept.
    private func saveWithLiveStates(_ id: Int, track: SubtitleTrack) async {
        var value = track
        let states = await liveStates(value.cues.flatMap(\.tokens))
        guard !states.isEmpty else { return }
        for index in value.cues.indices { value.cues[index].tokens = value.cues[index].tokens.updating(states) }
        saveSubtitles(id, track: value)
    }

    var videoBytes: Int64 { localEpisodes.values.reduce(0) { $0 + $1.sizeBytes } }

    func deleteEpisode(_ id: Int) {
        for url in [videoURL(id), cuesURL(id), metaURL(id)] { try? FileManager.default.removeItem(at: url) }
        loadLocalEpisodes()
    }

    /// Downloads episodes one after another (the Mac prepares each in turn).
    func downloadAll(_ items: [EpisodeItem]) {
        let pending = items.filter { item in localEpisodes[item.id] == nil && videoJobs[item.id] == nil && !videoQueue.contains { $0.id == item.id } }
        guard !pending.isEmpty else { return }
        let idle = videoQueue.isEmpty
        videoQueue.append(contentsOf: pending)
        guard idle else { return }
        Task {
            while let next = videoQueue.first {
                if localEpisodes[next.id] == nil { await download(next) }
                videoQueue.removeAll { $0.id == next.id }
            }
        }
    }

    /// Live Jiten states for words on screen (the Mac asks Jiten; cached there).
    func liveStates(_ tokens: [StudyToken]) async -> [String: StudyStateRow] {
        guard let client = app.macConnection, !app.macOffline else { return [:] }
        var seen = Set<String>(), pairs: [[Int]] = []
        for token in tokens where token.hasEntry && seen.insert(token.key).inserted { pairs.append([token.wordId, token.readingIndex]) }
        struct Request: Codable { let pairs: [[Int]] }
        var result: [String: StudyStateRow] = [:]
        for start in stride(from: 0, to: pairs.count, by: 500) {
            let chunk = Array(pairs[start..<min(pairs.count, start + 500)])
            guard let value = try? await client.media("study/states", post: Request(pairs: chunk), as: StudyStates.self) else { break }
            for row in value.rows { result[row.key] = row }
        }
        if !result.isEmpty { offlineWords.remember(states: Array(result.values)); saveOfflineWords() }
        return result
    }

    func cancelDownload(_ id: Int) {
        videoQueue.removeAll { $0.id == id }
        videoCancelled.insert(id)
        videoTasks[id]?.cancel()
        videoTasks[id] = nil
        videoJobs[id] = nil
    }

    func download(_ episode: EpisodeItem) async {
        guard videoJobs[episode.id] == nil else { return }
        videoCancelled.remove(episode.id)
        videoJobs[episode.id] = VideoJob(phase: "Preparing on the Mac…", fraction: 0)
        let background = BackgroundTaskBox()
        let episodeId = episode.id
        background.id = UIApplication.shared.beginBackgroundTask(withName: "Pudge video") { [weak self] in
            Task { @MainActor in
                self?.cancelDownload(episodeId)
                if background.id != .invalid { UIApplication.shared.endBackgroundTask(background.id); background.id = .invalid }
            }
        }
        defer {
            videoJobs[episode.id] = nil
            if background.id != .invalid { UIApplication.shared.endBackgroundTask(background.id); background.id = .invalid }
        }
        func checkCancelled() throws { if videoCancelled.contains(episode.id) { throw CancellationError() } }
        do {
            let client = try connection()
            guard var state = try await preparedVideo(episode, client: client) else { return }
            guard state.state == "ready", var video = state.video else {
                throw PudgeError.invalid(state.message.isEmpty ? "The Mac could not prepare this episode" : state.message)
            }
            videoJobs[episode.id] = VideoJob(phase: "Subtitles…", fraction: 0)
            let track = try await client.media("episodes/\(episode.id)/subtitles", as: SubtitleTrack.self)
            try checkCancelled()
            try FileManager.default.createDirectory(at: videoFolder, withIntermediateDirectories: true)
            var folder = videoFolder
            var values = URLResourceValues(); values.isExcludedFromBackup = true
            try? folder.setResourceValues(values)
            let free = (try? videoFolder.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage) ?? Int64.max
            guard Int64(free) > video.sizeBytes + 500_000_000 else { throw PudgeError.invalid("Not enough free space on this \(PudgeTheme.device)") }
            let partial = videoFolder.appendingPathComponent("\(episode.id).part")
            try checkCancelled()
            do {
                try await fetch("/api/v2/resources/\(video.resourceId)/content?sha256=\(video.sha256)", client: client, to: partial, id: episode.id)
            } catch let refused as MacRefusedFile {
                // The Mac's copy went away (removed for space, or the episode changed) after it said
                // "ready": it is prepared again once and downloaded from the new copy.
                AppLog.write("video", "episode \(episode.id): \(refused.localizedDescription); preparing it again")
                try checkCancelled()
                guard let again = try await preparedVideo(episode, client: client) else { return }
                guard again.state == "ready", let fresh = again.video else {
                    throw PudgeError.invalid(again.message.isEmpty ? "The Mac could not prepare this episode" : again.message)
                }
                state = again; video = fresh
                try await fetch("/api/v2/resources/\(video.resourceId)/content?sha256=\(video.sha256)", client: client, to: partial, id: episode.id)
            }
            try checkCancelled()
            videoJobs[episode.id] = VideoJob(phase: "Verifying…", fraction: 1)
            let target = videoURL(episode.id)
            let sha = video.sha256, size = video.sizeBytes
            try await Task.detached(priority: .utility) {
                try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size)
            }.value
            try checkCancelled()
            try? FileManager.default.removeItem(at: target)
            try FileManager.default.moveItem(at: partial, to: target)
            try JSONEncoder().encode(track).write(to: cuesURL(episode.id), options: .atomic)
            let local = LocalEpisode(id: episode.id, title: episode.title, number: episode.number, sha256: video.sha256,
                                     sizeBytes: video.sizeBytes, durationMs: state.durationMs ?? Int64(episode.duration * 1000),
                                     positionMs: Int64(episode.position * 1000), positionDirty: false, savedAt: Date().timeIntervalSince1970,
                                     mediaId: episode.mediaId, subtitleRevision: episode.subtitleRevision)
            try JSONEncoder().encode(local).write(to: metaURL(episode.id), options: .atomic)
            loadLocalEpisodes()
            AppLog.write("video", "downloaded episode \(episode.id) \(video.sizeBytes) bytes")
            let episodeId = episode.id
            Task {
                await saveWithLiveStates(episodeId, track: track)
                if await rememberWords(track.cues.flatMap(\.tokens)) { wordsChecked.insert(episodeId) }
            }
        } catch {
            try? FileManager.default.removeItem(at: videoFolder.appendingPathComponent("\(episode.id).part"))
            if !videoCancelled.contains(episode.id) {
                AppLog.write("video", "episode \(episode.id) not downloaded: \(error.localizedDescription)")
                app.message = error.localizedDescription
            }
        }
    }

    /// Asks the Mac to prepare the episode and waits until it is ready (nil: cancelled here).
    /// "idle" means the Mac restarted or removed its copy meanwhile: it is asked again. A Mac
    /// that does not answer for a moment (restarting, Wi-Fi hiccup) is waited for about a minute.
    private func preparedVideo(_ episode: EpisodeItem, client: MacConnection) async throws -> EpisodeState? {
        let route = "episodes/\(episode.id)/prepare"
        var state = try await client.media(route, post: EmptyRequest(), as: EpisodeState.self)
        var unanswered = 0
        while state.state == "preparing" || state.state == "idle" {
            if videoCancelled.contains(episode.id) { return nil }
            videoJobs[episode.id] = VideoJob(phase: "Preparing on the Mac…", fraction: state.progress)
            try await Task.sleep(nanoseconds: 2_000_000_000)
            do {
                if state.state == "idle" {
                    state = try await client.media(route, post: EmptyRequest(), as: EpisodeState.self)
                } else {
                    state = try await client.media("episodes/\(episode.id)", as: EpisodeState.self)
                }
                unanswered = 0
            } catch {
                unanswered += 1
                guard TransferRetry.isTransient(error), unanswered < 30 else { throw error }
            }
        }
        return state
    }

    private func fetch(_ route: String, client: MacConnection, to target: URL, id: Int) async throws {
        defer { videoTasks[id] = nil }
        try await transfer(route, client: client, to: target, cancelled: { self.videoCancelled.contains(id) },
                           started: { [weak self] task in self?.videoTasks[id] = task }) { [weak self] value in
            if self?.videoJobs[id] != nil { self?.videoJobs[id] = VideoJob(phase: "Downloading…", fraction: value) }
        }
    }

    /// Downloads a file the Mac prepared (pinned TLS, cellular only when allowed in Settings).
    /// A cut connection (Wi-Fi drop, the Mac busy or restarting) is tried again a few times and
    /// resumes where it stopped (TransferRetry) instead of failing with a network error.
    private func transfer(_ route: String, client: MacConnection, to target: URL, cancelled: () -> Bool = { false },
                          started: @escaping (URLSessionTask) -> Void, progress: @escaping @MainActor (Double) -> Void) async throws {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 6 * 3600
        config.allowsCellularAccess = UserDefaults.standard.bool(forKey: "pudge.cellularDownloads")
        let delegate = PinnedDelegate(pin: client.delegate.pin, origin: client.delegate.origin)
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let request = try client.request(route)
        let name = route.split(separator: "?").first.map(String.init) ?? route
        var resumeData: Data?
        var attempt = 1
        while true {
            do {
                try await transferAttempt(session: session, request: request, resumeData: resumeData, to: target,
                                          started: started, progress: progress)
                if attempt > 1 { AppLog.write("transfer", "\(name) done after \(attempt) attempts") }
                return
            } catch {
                guard TransferRetry.shouldRetry(error, attempt: attempt), !cancelled() else {
                    AppLog.write("transfer", "\(name) failed (attempt \(attempt)): \(error.localizedDescription)")
                    throw error
                }
                resumeData = (error as? URLError)?.downloadTaskResumeData
                AppLog.write("transfer", "\(name) cut (\((error as NSError).code)): attempt \(attempt + 1) "
                             + (resumeData == nil ? "from the start" : "resumes"))
                try await Task.sleep(nanoseconds: UInt64(TransferRetry.delay(after: attempt) * 1_000_000_000))
                if cancelled() { throw CancellationError() }
                attempt += 1
            }
        }
    }

    private func transferAttempt(session: URLSession, request: URLRequest, resumeData: Data?, to target: URL,
                                 started: @escaping (URLSessionTask) -> Void, progress: @escaping @MainActor (Double) -> Void) async throws {
        var observation: NSKeyValueObservation?
        defer { observation?.invalidate() }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let finished: @Sendable (URL?, URLResponse?, Error?) -> Void = { location, response, error in
                if let error { continuation.resume(throwing: error); return }
                // 206: a resumed download (URLSession hands over the whole file).
                guard let location, let http = response as? HTTPURLResponse, [200, 206].contains(http.statusCode) else {
                    continuation.resume(throwing: MacRefusedFile(status: (response as? HTTPURLResponse)?.statusCode ?? 0)); return
                }
                do {
                    try? FileManager.default.removeItem(at: target)
                    try FileManager.default.moveItem(at: location, to: target)
                    continuation.resume()
                } catch { continuation.resume(throwing: error) }
            }
            let task = resumeData.map { session.downloadTask(withResumeData: $0, completionHandler: finished) }
                ?? session.downloadTask(with: request, completionHandler: finished)
            let throttle = ProgressThrottle()
            observation = task.progress.observe(\.fractionCompleted) { fraction, _ in
                let value = fraction.fractionCompleted
                guard value - throttle.last >= 0.01 || value >= 1 else { return }
                throttle.last = value
                Task { @MainActor in progress(value) }
            }
            started(task)
            task.resume()
        }
    }

    func saveVideoPosition(_ id: Int, positionMs: Int64) {
        guard var item = localEpisodes[id] else { return }
        item.positionMs = max(0, positionMs); item.positionDirty = true; item.savedAt = Date().timeIntervalSince1970
        localEpisodes[id] = item
        try? JSONEncoder().encode(item).write(to: metaURL(id), options: .atomic)
    }

    /// Merges played seconds; once 65% of the episode (at least 3 min) was really played and the
    /// end is reached, the episode counts like a full watch in mpv (AniList, schedule, cleanup).
    func recordPlayed(_ id: Int, seconds: IndexSet, positionMs: Int64) {
        guard var item = localEpisodes[id] else { return }
        var played = item.watchedSeconds ?? IndexSet()
        played.formUnion(seconds)
        item.watchedSeconds = played
        let duration = Double(item.durationMs) / 1000
        let required = duration > 0 ? max(180, duration * 0.65) : 300
        if item.watchedSent == nil, duration > 0, Double(played.count) >= required, Double(positionMs) / 1000 >= duration * 0.85 {
            item.watchedSent = false
            AppLog.write("video", "episode \(id) watched on this device (\(played.count) s played)")
        }
        localEpisodes[id] = item
        try? JSONEncoder().encode(item).write(to: metaURL(id), options: .atomic)
        if item.watchedSent == false { Task { await sendVideoPositions() } }
    }

    private var sendingPositions = false
    private var sendAgain = false

    func sendVideoPositions() async {
        guard let client = app.macConnection, !app.macOffline else { return }
        guard !sendingPositions else { sendAgain = true; return }
        sendingPositions = true
        defer {
            sendingPositions = false
            if sendAgain { sendAgain = false; Task { await sendVideoPositions() } }
        }
        for item in localEpisodes.values where item.watchedSent == false {
            do {
                let result = try await client.media("episodes/\(item.id)/watched",
                    post: EpisodeWatchedRequest(duration: Double(item.durationMs) / 1000, activeSeconds: Double(item.watchedSeconds?.count ?? 0)),
                    as: StudyActionResult.self)
                if var sent = localEpisodes[item.id] {
                    sent.watchedSent = true
                    localEpisodes[item.id] = sent
                    try? JSONEncoder().encode(sent).write(to: metaURL(item.id), options: .atomic)
                }
                if !result.ok { app.message = result.message }
                AppLog.write("video", "episode \(item.id) watched → Mac: \(result.outcome)")
            } catch { break }
        }
        for item in localEpisodes.values where item.positionDirty {
            do {
                _ = try await client.media("episodes/\(item.id)/position",
                    post: EpisodePositionRequest(seconds: Double(item.positionMs) / 1000, duration: Double(item.durationMs) / 1000,
                                                 observedAt: item.savedAt), as: OkResponse.self)
                if var sent = localEpisodes[item.id] {
                    if sent.positionMs == item.positionMs { sent.positionDirty = false }
                    localEpisodes[item.id] = sent
                    try? JSONEncoder().encode(sent).write(to: metaURL(item.id), options: .atomic)
                }
            } catch { return }
        }
    }
}

/// Statistics on the iPhone. A reader or the video player reports what is on screen every 5 s;
/// each step becomes an interval for the Mac. Same rules as the Mac: reading counts while the
/// app is in front and was touched in the last 5 minutes, video while it plays. Audiobooks are
/// counted on the Mac from the listening checkpoints.
@MainActor
final class ActivityRecorder {
    private struct Sample { let uptime: TimeInterval; let wall: Date; let position: [String: Double] }
    private let store: LocalStore
    private var samples: [String: Sample] = [:]
    private var lastTouch = ProcessInfo.processInfo.systemUptime

    init(store: LocalStore) { self.store = store }

    private var touchedKey: String?

    /// A tap, scroll or page turn in the reader of this item.
    func touched(kind: String, item: Int) { lastTouch = ProcessInfo.processInfo.systemUptime; touchedKey = "\(kind):\(item)" }
    /// This item was the last one touched, less than 5 minutes ago (a reader left open under another one stops counting).
    func isReading(kind: String, item: Int) -> Bool {
        ProcessInfo.processInfo.systemUptime - lastTouch <= 300 && touchedKey == "\(kind):\(item)"
    }

    /// What is on screen now; `active` false ends the current run (idle, paused, in background).
    func observe(kind: String, item: Int, active: Bool, at position: [String: Double]) {
        let key = "\(kind):\(item)"
        let uptime = ProcessInfo.processInfo.systemUptime, wall = Date()
        guard active, UIApplication.shared.applicationState == .active else { samples[key] = nil; return }
        defer { samples[key] = Sample(uptime: uptime, wall: wall, position: position) }
        guard let previous = samples[key] else { return }
        let elapsed = uptime - previous.uptime, clock = wall.timeIntervalSince(previous.wall)
        // A long gap is a locked phone or a stalled timer; a clock jump is a time change.
        guard elapsed >= 0.15, elapsed <= 15, clock > 0, abs(clock - elapsed) <= 2.5 else { return }
        let interval = ActivityInterval(kind: kind, item: item, start: previous.wall, end: wall,
                                        active: elapsed, from: previous.position, to: position)
        do { try store.enqueueActivity(interval) }
        catch { AppLog.write("stats", "activity not saved: \(error.localizedDescription)") }
    }

    /// The screen closed: the next observation of this item starts a new run.
    func stop(kind: String, item: Int) { samples["\(kind):\(item)"] = nil }
}

