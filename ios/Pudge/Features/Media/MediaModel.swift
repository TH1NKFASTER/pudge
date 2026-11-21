import Foundation
import AVFoundation
import CoreMedia
import ImageIO
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
/// offline) never takes a newer place from another iPhone/iPad or the Mac back. `sentAt`: this
/// phone's clock when sending, so the Mac corrects a clock that runs ahead (it took the Mac's
/// newer place as older).
struct NovelPositionRequest: Codable { let chapter: Int; let offset: Double; let finished: Bool?; var observedAt: Double? = Date().timeIntervalSince1970; var sentAt: Double? = Date().timeIntervalSince1970; var paragraph: Int? = nil
    /// Only Finished (Mac capability "ln-finish"): the place and the read marker stay as they are.
    var finishOnly: Bool? = nil }
/// The "read up to here" marker: `kind` "manual" (Read up to here) or "auto" (the audio reads
/// there); the Mac applies its rules and answers with the marker as it is now.
struct ReadMarkerRequest: Codable { let chapter: Int; let paragraph: Int; let paragraphOffset: Int; let kind: String; var observedAt: Double? = Date().timeIntervalSince1970; var sentAt: Double? = Date().timeIntervalSince1970; var markerId: String? = nil }
/// A read marker set while the Mac did not answer, sent when it does (`read-markers.pending.json`).
/// `markerId` stays the same on every resend: one the Mac already applied changes nothing there.
struct PendingReadMarker: Codable { let markerId: String; let book: Int; var request: ReadMarkerRequest; let serverId: String? }
/// A light-novel position or marker answer; `marker` from Macs that keep one.
struct ReadMarkerResponse: Decodable { let ok: Bool?; let marker: ReadMarker? }
struct MangaPositionRequest: Codable { let page: Int; let read: Bool; var observedAt: Double? = Date().timeIntervalSince1970; var sentAt: Double? = Date().timeIntervalSince1970; var readThrough: Int? = nil }
/// `ranges`: the parts played on this device (`[[start, end]]`, seconds) until the watch counted
/// here; the Mac sums them with mpv's (and other devices') for the watched credit. Older Macs ignore it.
struct EpisodePositionRequest: Codable { let seconds: Double; let duration: Double; var observedAt: Double? = nil; var ranges: [[Double]]? = nil; var sentAt: Double? = Date().timeIntervalSince1970 }
/// `counted`: the parts watched here and on the Mac together counted the episode there.
/// `finale`: the counted watch ended the anime (the score wheel, AnimeFinaleModel).
struct EpisodePositionReceipt: Decodable { let ok: Bool?; let counted: Bool?; var finale: AnimeFinale? = nil }
struct EmptyRequest: Codable {}
struct OkResponse: Decodable { let ok: Bool? }
struct SubtitleDelayRequest: Codable { let ms: Int }

/// The player's subtitle timing per episode (SubtitleDelays), kept in UserDefaults; its own object
/// so the timing sheet does not redraw with every change of the media model (a redraw while the
/// wheel turned set it back to the old value: the wheel snapped back to 0).
@MainActor
final class SubtitleDelayStore: ObservableObject {
    private static let key = "pudge.subtitleDelays"
    @Published private(set) var delays: SubtitleDelays

    init() {
        delays = UserDefaults.standard.data(forKey: Self.key).flatMap { try? JSONDecoder().decode(SubtitleDelays.self, from: $0) }
            ?? SubtitleDelays()
    }

    func ms(_ id: Int) -> Int { delays.ms(id) }

    /// Set in the player; sent to the Mac with the next positions (or right away when it answers).
    func set(_ ms: Int, for id: Int) {
        var value = delays
        guard value.set(ms, for: id) else { return }
        update(value)
    }

    func adopt(mac ms: Int?, for id: Int) {
        var value = delays
        guard value.adopt(mac: ms, for: id) else { return }
        update(value)
    }

    func sent(_ ms: Int, for id: Int) {
        var value = delays
        value.sent(ms, for: id)
        guard value != delays else { return }
        update(value)
    }

    private func update(_ value: SubtitleDelays) {
        delays = value
        if let data = try? JSONEncoder().encode(value) { UserDefaults.standard.set(data, forKey: Self.key) }
    }
}

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
    /// The Mac's id (below zero: an episode of a previous Mac, see DownloadOwner).
    var id: Int
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
    /// The Mac it came from (nil: downloaded before this was kept; taken as the bound Mac).
    var serverId: String?
    /// When the watch counted on this device (seconds): the download is deleted after the
    /// Mac's delay (WatchedCleanup), unless `kept` (Keep tapped, or downloaded again after a watch).
    var watchedAt: Double?
    var kept: Bool?
    /// Watched on the Mac or another device (adoptMacWatches): deleted after the delay unless
    /// opened here again (WatchedCleanupState.watchedElsewhere).
    var watchedElsewhere: Bool?
    /// The copy downloaded (VideoQuality raw value; nil: the original, before qualities).
    var quality: String?
    /// The Mac's detected opening/ending of the file (the player's Skip button, offline too).
    var skipSegments: [SkipSegment]?
    /// Something here the Mac has not got yet.
    var unsent: Bool { watchedSent == false || positionDirty }
}

/// A light novel or manga volume kept on the iPhone for reading without the Mac
/// (`reading/<kind>-<id>/download.json`); the position read here waits for the Mac.
struct ReadingDownload: Codable, Hashable {
    let kind: String
    /// The Mac's id (below zero: a volume of a previous Mac, see DownloadOwner).
    var id: Int
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
    /// Manga in two pages: `page + 1`, the second page of the pair on screen (read too).
    var readThrough: Int?
    /// Read here since the Mac last confirmed the position; `positionAt`: when (seconds).
    var positionDirty: Bool?
    var positionAt: Double?
    /// Deleted on the Mac (seconds, the Mac's time of deletion): kept readable and marked;
    /// removed a week later unless opened since (`openedAt`).
    var deletedOnMacAt: Double?
    var openedAt: Double?
    /// The Mac it came from (nil: downloaded before this was kept; taken as the bound Mac).
    var serverId: String?
    var key: String { kind + ":" + String(id) }
}

/// GET media/anime: the groups and the Mac's delay for deleting watched episodes (hours).
struct AnimeListResponse: Decodable {
    let items: [AnimeGroup]
    let deleteWatchedAfterHours: Double?
    /// The Mac Home's Caught up / Waiting titles (nil from an older Mac).
    let home: MacHomeShelves?
}

struct EpisodeWatchedRequest: Codable { let duration: Double; let activeSeconds: Double }

struct GateReviewRequest: Codable { let wordId: Int; let readingIndex: Int; let grade: String; let attemptId: String }

private final class ProgressThrottle: @unchecked Sendable { var last = 0.0 }

/// The Mac answered a file download with an error (its copy is gone or changed).
struct MacRefusedFile: LocalizedError {
    let status: Int
    var errorDescription: String? { "The Mac did not send the file (HTTP \(status))" }
}
/// What AVFoundation says about a video file (read off the main thread): a download's check and
/// the player's log.
enum VideoFileProbe {
    static func probe(_ url: URL) async -> (MediaProbe, String) {
        let asset = AVURLAsset(url: url)
        do {
            let (playable, duration) = try await asset.load(.isPlayable, .duration)
            let videos = try await asset.loadTracks(withMediaType: .video)
            let audios = try await asset.loadTracks(withMediaType: .audio)
            var decodable = true
            var format = ""
            if let track = videos.first {
                decodable = (try? await track.load(.isDecodable)) ?? true
                format = await describe(track)
            }
            let seconds = duration.isNumeric ? duration.seconds : nil
            return (MediaProbe(isPlayable: playable, hasVideo: !videos.isEmpty, hasAudio: !audios.isEmpty,
                               videoDecodable: decodable, durationSeconds: seconds), format)
        } catch {
            return (MediaProbe(isPlayable: false, hasVideo: false, hasAudio: false, durationSeconds: nil),
                    "not read: " + error.localizedDescription)
        }
    }

    /// "avc1 1920x1080 High@4.1" (the H.264 level from its avcC box).
    static func describe(_ track: AVAssetTrack) async -> String {
        guard let descriptions = try? await track.load(.formatDescriptions), let first = descriptions.first else { return "" }
        let code = CMFormatDescriptionGetMediaSubType(first)
        let codec = String([24, 16, 8, 0].map { Character(UnicodeScalar(UInt8((code >> UInt32($0)) & 0xff))) })
        let size = CMVideoFormatDescriptionGetDimensions(first)
        var text = "\(codec) \(size.width)x\(size.height)"
        let atoms = CMFormatDescriptionGetExtension(first, extensionKey: kCMFormatDescriptionExtension_SampleDescriptionExtensionAtoms) as? NSDictionary
        if let avcC = atoms?["avcC"] as? Data, let value = VideoFormatInfo.avcProfileLevel([UInt8](avcC)) {
            text += " " + VideoFormatInfo.describe(profile: value.profile, level: value.level)
        }
        return text
    }

    static func summary(_ probe: MediaProbe) -> String {
        let duration = probe.durationSeconds.map { String(format: "%.1f s", $0) } ?? "no duration"
        return "playable=\(probe.isPlayable) video=\(probe.hasVideo) audio=\(probe.hasAudio) decodable=\(probe.videoDecodable) \(duration)"
    }
}

/// A resumed download answered with other bytes than asked for (RangedResume.Decision.startOver).
struct RangeMismatch: LocalizedError {
    let status: Int
    let contentRange: String
    let offset: Int64
    var errorDescription: String? { "The Mac sent other bytes than asked for (HTTP \(status), \(contentRange) for \(offset))" }
}
final class BackgroundTaskBox: @unchecked Sendable { var id = UIBackgroundTaskIdentifier.invalid }

/// Pictures decoded off the main thread at the size they are shown (an ImageIO thumbnail,
/// decoded at once): a full-size manga page decoded on the main thread made page turns stutter.
enum DownsampledImage {
    /// Twice the screen's longer side in pixels: sharp enough when a page is zoomed in.
    @MainActor static func screenLimit() -> Int {
        let screen = UIScreen.main
        return Int(max(screen.bounds.width, screen.bounds.height) * screen.scale * 2)
    }
    /// The picture at most `maxPixels` on its longer side (never enlarged), turned upright.
    static func load(_ source: CGImageSource?, maxPixels: Int) -> UIImage? {
        guard let source, CGImageSourceGetCount(source) > 0 else { return nil }
        var limit = max(1, maxPixels)
        if let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
           let width = properties[kCGImagePropertyPixelWidth] as? Int, let height = properties[kCGImagePropertyPixelHeight] as? Int,
           width > 0, height > 0 {
            limit = min(limit, max(width, height))
        }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: limit,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return UIImage(cgImage: image)
    }
    static func load(_ url: URL, maxPixels: Int) -> UIImage? {
        load(CGImageSourceCreateWithURL(url as CFURL, nil), maxPixels: maxPixels)
    }
    static func load(_ data: Data, maxPixels: Int) -> UIImage? {
        load(CGImageSourceCreateWithData(data as CFData, nil), maxPixels: maxPixels)
    }
    /// Bytes of a decoded picture (its pixels × 4), an image cache's cost.
    static func cost(_ image: UIImage) -> Int {
        let pixels = Double(image.size.width * image.scale) * Double(image.size.height * image.scale)
        return pixels.isFinite ? Int(min(pixels * 4, Double(Int32.max))) : 0
    }
}

/// One (ranged) GET of a file the Mac prepared, written straight into the partial file on disk.
/// What arrived stays there when the connection is cut, so the next request asks for the rest.
/// The session's serial delegate queue calls it, one callback at a time.
private final class RangedFileDelegate: PinnedDelegate, URLSessionDataDelegate, @unchecked Sendable {
    private let target: URL
    private let offset: Int64
    private let total: Int64
    /// The copy's SHA-256: a player watching it meanwhile reads the bytes written (PartialFileShare).
    private let key: String
    private let report: (Int64) -> Void
    private var finish: ((Error?) -> Void)?
    private var handle: FileHandle?
    private var written: Int64 = 0
    private var failure: Error?
    private var complete = false

    init(pin: String, origin: URL, target: URL, offset: Int64, total: Int64, key: String,
         report: @escaping (Int64) -> Void, finish: @escaping (Error?) -> Void) {
        self.target = target; self.offset = offset; self.total = total; self.key = key; self.report = report; self.finish = finish
        super.init(pin: pin, origin: origin)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let http = response as? HTTPURLResponse
        let status = http?.statusCode ?? 0
        let decision = RangedResume.decide(status: status, contentRange: http?.value(forHTTPHeaderField: "Content-Range"),
                                           offset: offset, total: total)
        do {
            switch decision {
            case .append:
                // Only the bytes before `offset` count while the file is trimmed to them.
                PartialFileShare.shared.advance(key, url: target, to: min(offset, PartialFileShare.shared.validBytes(key)))
                let file = try FileHandle(forWritingTo: target)
                try file.truncate(atOffset: UInt64(offset))
                _ = try file.seekToEnd()
                handle = file; written = offset
                PartialFileShare.shared.advance(key, url: target, to: offset)
            case .restart:
                // The whole file comes (another edition, or the Mac ignored the range): from the start.
                // A player reading the old bytes stops using them before the file is cut.
                PartialFileShare.shared.reset(key, url: target)
                guard FileManager.default.createFile(atPath: target.path, contents: nil,
                                                     attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]) else {
                    throw CocoaError(.fileWriteUnknown)
                }
                handle = try FileHandle(forWritingTo: target); written = 0
            case .complete:
                complete = true; completionHandler(.cancel); return
            case .refuse:
                failure = MacRefusedFile(status: status); completionHandler(.cancel); return
            case .startOver:
                // Other bytes than asked for: nothing is written; the partial goes and it starts from 0.
                failure = RangeMismatch(status: status, contentRange: http?.value(forHTTPHeaderField: "Content-Range") ?? "-", offset: offset)
                completionHandler(.cancel); return
            }
        } catch {
            failure = error; completionHandler(.cancel); return
        }
        report(written)
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard failure == nil, let handle else { return }
        do {
            try handle.write(contentsOf: data)
            written += Int64(data.count)
            PartialFileShare.shared.advance(key, url: target, to: written)
            report(written)
        } catch {
            failure = error; dataTask.cancel()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        try? handle?.close(); handle = nil
        let result: Error? = failure ?? (complete ? nil : error)
        finish?(result); finish = nil
    }
}

struct VideoJob: Equatable {
    var phase: String
    var fraction: Double
}

/// The copy an episode download brings (ready on the Mac): watched meanwhile, the same copy.
struct DownloadingCopy: Equatable {
    let video: ResourceRef
    let quality: VideoQuality
}

/// An episode watched from the Mac (Watch): its record (place, played seconds), the Mac's
/// subtitles and the stream the player plays.
final class StreamPlayback: Identifiable {
    let item: EpisodeItem
    let record: LocalEpisode
    let track: SubtitleTrack
    let stream: MacVideoStream
    init(item: EpisodeItem, record: LocalEpisode, track: SubtitleTrack, stream: MacVideoStream) {
        self.item = item; self.record = record; self.track = track; self.stream = stream
    }
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
    /// `novels` is the Mac's list (not only the volumes on this device): the Light Novels tab
    /// asks the Mac only while it is not. A finished audiobook's offer filled `novels` with the
    /// downloaded volumes first, and the tab took that for loaded and never asked the Mac.
    @Published private(set) var novelsFromMac = false
    @Published var manga: [MangaItem] = []
    @Published var episodes: [EpisodeItem] = []
    /// Anime as the Mac's Anime page offers them (schedule-aware).
    @Published var animeGroups: [AnimeGroup] = []
    /// The Mac Home's titles with nothing to play yet (Caught up, Waiting…); nil: an older Mac.
    @Published var macHome: MacHomeShelves?
    /// Episodes watched on this device (by episode id) that the Mac's list may not show yet.
    @Published private(set) var watchedHere: [Int: LocalAnimeWatch] = [:]
    /// When the shown anime list was asked for (seconds since 1970; 0: none yet).
    private(set) var animeListRequestedAt: Double = 0
    /// The lists as shown: a watch on this device counts at once ("Watched 3 of 25" after episode 3),
    /// before the Mac's next list has it (AnimeProgressOverlay).
    var shownMacHome: MacHomeShelves? { AnimeProgressOverlay.apply(macHome, watches: Array(watchedHere.values), listRequestedAt: animeListRequestedAt) }
    func shown(_ groups: [AnimeGroup]) -> [AnimeGroup] {
        AnimeProgressOverlay.apply(groups, watches: Array(watchedHere.values), listRequestedAt: animeListRequestedAt)
    }
    /// Episodes waiting for "Download all" (one download at a time).
    @Published var videoQueue: [EpisodeItem] = []
    @Published var localEpisodes: [Int: LocalEpisode] = [:]
    @Published var videoJobs: [Int: VideoJob] = [:]
    /// The episodes of `videoJobs` (a second copy of the same episode is not downloaded meanwhile).
    private var videoJobItems: [Int: EpisodeItem] = [:]
    /// Downloads whose copy is ready on the Mac and coming to this device: Play starts at once
    /// from the part already here and takes the rest from the Mac (play while downloading).
    @Published private(set) var downloadingCopies: [Int: DownloadingCopy] = [:]
    /// Subtitle timing per episode (the player's wheel), kept here and sent to the Mac.
    let subtitleDelays = SubtitleDelayStore()
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
    /// The words graded or added on this device a moment ago, with their new states: the
    /// subtitles, the reader and looked-up texts recolour every occurrence at once.
    @Published private(set) var gradedWords = GradedWords()
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
    /// When the shown Statistics were saved on this device (nil: the Mac's answer of now).
    @Published var statsSavedAt: Date?
    /// The period the Statistics screen shows now (an answer for another one is dropped).
    private var statsPeriodWanted = ""
    /// The shown Planning came from this device (the Mac's answer is still asked for).
    private var planningFromCache = false
    private var prefetching = false
    /// The last answers of the heavy screens (Statistics per period, Planning), shown at once on open.
    private lazy var screenCache = ScreenCache(directory: app.store.directory.appendingPathComponent("screen-cache", isDirectory: true))
    private var words: [String: WordDetail] = [:]
    /// Meanings and states from the last updates, for word taps without the Mac.
    static let offlineWordsFile = "offline-words.json"
    private lazy var offlineWords: OfflineWords = { self.offlineWordsLoaded = true; return OfflineWords.load(self.offlineWordsURL) }()
    /// `offlineWords` is in memory (read at once on first use, or by preloadOfflineWords).
    private var offlineWordsLoaded = false
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
    /// Removed downloads whose place or "watched" mark the Mac has not got yet (sent later).
    private var pendingEpisodes: [Int: LocalEpisode] = [:]
    /// Episodes open in the player now (never deleted automatically meanwhile).
    private var playingEpisodes = Set<Int>()
    /// A player is open (the score wheel waits until it closed).
    var playerOnScreen: Bool { !playingEpisodes.isEmpty }
    /// The end of an anime watched here: the score wheel and the next entry.
    private(set) lazy var finales = AnimeFinaleModel(media: self)
    /// Episodes being made ready to watch from the Mac (the Mac's preparation, as for a download).
    @Published var streamJobs: [Int: VideoJob] = [:]
    private var streamCancelled = Set<Int>()
    /// Episodes playing from the Mac now: place and played seconds as for a download, kept on
    /// this device only while the Mac has not got them (in `pendingEpisodes`, like a deleted download's).
    private var streamRecords: [Int: LocalEpisode] = [:]
    private var pendingReading: [String: ReadingDownload] = [:]
    /// Read markers the Mac has not got yet, oldest first: every manual one, the newest automatic one per volume.
    private lazy var pendingMarkers: [PendingReadMarker] = Self.loadPendingMarkers(pendingMarkersURL)
    private var pendingMarkersURL: URL { app.store.directory.appendingPathComponent("read-markers.pending.json") }

    init(app: PudgeModel) {
        self.app = app
        images.countLimit = 60
        // Manga pages are full-size pictures (~12 MB each decoded): 60 of them were ~700 MB.
        images.totalCostLimit = 160 * 1024 * 1024
        loadLocalEpisodes()
        loadReadingDownloads()
        pruneDeletedReading()
        network.start(queue: .main)
    }

    func connection() throws -> MacConnection {
        guard let connection = app.macConnection else { throw PudgeError.invalid("Connect a Mac in Settings") }
        return connection
    }

    // MARK: The Mac downloads belong to

    /// The Mac this device's data belongs to (bound on pairing, moved by "Switch to this Mac").
    private var boundServer: String? { try? app.store.boundMac() }
    /// Downloaded from another Mac than the bound one (switched since): kept playable and readable
    /// here, never synced. Its ids name other episodes and volumes on this Mac (every Mac counts
    /// from 1): its marks went to this Mac's episode of that number, its subtitles and updates
    /// came from there.
    func isOtherMac(_ serverId: String?) -> Bool {
        guard let serverId, let bound = boundServer else { return false }
        return serverId != bound
    }
    // An id below zero was renumbered when its Mac stopped being the bound one: it stays another
    // Mac's after switching back (its serverId may be the bound Mac again), never synced.
    func isOtherMac(_ episode: LocalEpisode) -> Bool { episode.id < 0 || isOtherMac(episode.serverId) }
    func isOtherMac(_ record: ReadingDownload) -> Bool { record.id < 0 || isOtherMac(record.serverId) }
    private func isOtherMacVolume(_ kind: String, _ id: Int) -> Bool {
        readingDownloads["\(kind):\(id)"].map { isOtherMac($0) } ?? false
    }

    /// After "Switch to this Mac": downloads of the previous Mac leave this Mac's numbers, and the
    /// previous Mac's lists and "removed" marks go (the lists load again from this Mac).
    func macSwitched() {
        readingRemoved = []
        novels = []; manga = []; animeGroups = []; macHome = nil; episodes = []; videoQueue = []; animeListTag = nil
        novelsFromMac = false
        // The previous Mac's statistics and plans are not this one's.
        screenCache.clear(); stats = nil; statsSavedAt = nil; planning = nil
        loadLocalEpisodes()
        loadReadingDownloads()
    }

    // MARK: Library

    // Without Wi-Fi they say so instead of "Mac unavailable" (PudgeModel.unavailable).
    static var readingOffline: String { PudgeModel.unavailable("volumes downloaded to the \(PudgeTheme.device) open offline") }
    static var episodesOffline: String { PudgeModel.unavailable("downloaded episodes play offline") }

    /// The Mac answers again: lists, subtitles and saved word data are refreshed.
    func reconnected() async {
        await refreshEpisodes()
        await resumeInterruptedDownloads()
        if readingError != nil || !readingDownloads.isEmpty { await refreshReading() }
        if planningError != nil || planning == nil || planningFromCache { await refreshPlanning() }
        Task(priority: .background) { await self.prefetchHeavyScreens() }
    }

    /// After connecting: the Statistics of the chosen period are asked for at low priority and
    /// kept on the device only (not in memory), so the screen opens at once later. One request
    /// at a time; a recent saved copy is not asked for again.
    func prefetchHeavyScreens() async {
        guard !prefetching, !app.macOffline else { return }
        prefetching = true; defer { prefetching = false }
        let period = UserDefaults.standard.string(forKey: "pudge.statsPeriod") ?? "30d"
        let key = "stats-" + period, server = boundServer
        guard stats?.period != period, ScreenCache.needsPrefetch(savedAt: screenCache.savedAt(key, server: server)),
              let value = try? await connection().media("stats?period=" + period, as: StatsSummary.self) else { return }
        screenCache.save(value, key: key, server: server)
        AppLog.write("stats", "statistics (\(period)) kept for a quick open")
    }

    /// `forceUpdates`: pull to refresh checks downloaded volumes for changes now.
    func refreshReading(forceUpdates: Bool = false) async {
        loadingReading = true; defer { loadingReading = false }
        // Pull to refresh asks the Mac now; a tab shows the downloaded volumes at once, not after the Mac.
        if forceUpdates { app.retryMacNow() }
        if novels.isEmpty && manga.isEmpty { novels = savedNovelItems(); manga = savedMangaItems() }
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
            novelsFromMac = true
            manga = try await client.media("manga", as: ItemsResponse<MangaItem>.self).items
            adoptMacReadingPlaces()
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

    /// The light-novel list for a finished audiobook's "Mark light novel read" when the Read tab
    /// has not loaded it yet: the volumes on this device at once, then the Mac's list.
    func loadNovelListIfNeeded() async {
        guard !novelsFromMac, !loadingReading else { return }
        if novels.isEmpty { novels = savedNovelItems() }
        guard !app.macOffline, let client = try? connection(),
              let items = try? await client.media("ln", as: ItemsResponse<LightNovelItem>.self).items,
              !loadingReading, !novelsFromMac else { return }
        novels = items + savedNovelItems().filter { saved in !items.contains { $0.id == saved.id } }
        novelsFromMac = true
    }

    /// Marks a light novel read from its finished audiobook (the reader's Finish volume, AniList
    /// included); the volume's place and read marker stay. A Mac with "ln-finish" takes Finished
    /// alone (`finishOnly`, the real time). An older Mac only has the place route: the list's place
    /// is sent as an old one (observedAt 1), which it keeps unless the volume has no place yet.
    func markNovelFinished(_ novel: LightNovelItem) async -> Bool {
        guard !app.macOffline, let client = try? connection() else { return false }
        var request = NovelPositionRequest(chapter: novel.chapter, offset: novel.offset, finished: true)
        if app.macSupports("ln-finish") { request.finishOnly = true } else { request.observedAt = 1 }
        guard (try? await client.media("ln/\(novel.id)/position", post: request, as: OkResponse.self)) != nil else { return false }
        if let items = try? await client.media("ln", as: ItemsResponse<LightNovelItem>.self).items {
            novels = items + savedNovelItems().filter { saved in !items.contains { $0.id == saved.id } }
            novelsFromMac = true
        }
        return true
    }

    /// While the app is in front: the anime list again now and then, so a download that finished on
    /// the Mac shows here without reopening the app (AnimeLiveRefresh: 15 s while a download runs on
    /// the Mac, else 90 s). Not while a video plays, a reader is open or was just touched, or the Mac
    /// is away. A tick is cheap: the list only when the Mac's answer changed (ETag), no spinner, no
    /// subtitle or word updates (those come with a full refresh). Ends when cancelled.
    func liveRefreshLoop() async {
        while !Task.isCancelled {
            let seconds = AnimeLiveRefresh.interval(home: macHome)
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            if Task.isCancelled { return }
            let activity = app.activity
            guard AnimeLiveRefresh.shouldRefresh(macAvailable: app.macAvailable, playerOnScreen: playerOnScreen,
                                                 readerOnScreen: activity.readerOnScreen, refreshing: episodesRefresh != nil,
                                                 secondsSinceTouch: activity.secondsSinceTouch) else { continue }
            await refreshEpisodes(live: true)
        }
    }

    /// The running list refresh: another caller waits for it instead of asking the Mac again.
    private var episodesRefresh: Task<Void, Never>?
    /// The ETag of the anime list shown (a live tick asks "changed since?").
    private var animeListTag: String?

    /// `live`: a tick of liveRefreshLoop (quiet, only a changed list is applied).
    func refreshEpisodes(live: Bool = false) async {
        if let running = episodesRefresh {
            let quiet = episodesRefreshLive
            await running.value
            // A quiet tick ran: a full refresh (pull to refresh, back from the Mac) still follows.
            guard quiet, !live else { return }
            if let next = episodesRefresh { await next.value; return }
        }
        let task = Task { await self.loadEpisodes(live: live); self.episodesRefresh = nil }
        episodesRefresh = task
        episodesRefreshLive = live
        await task.value
    }
    private var episodesRefreshLive = false

    private func loadEpisodes(live: Bool) async {
        if !live { loadingEpisodes = true }
        defer { if loadingEpisodes { loadingEpisodes = false } }
        var perf = PerfSteps(live ? "anime list (live)" : "anime list", at: ProcessInfo.processInfo.systemUptime)
        defer { if let line = perf.report(at: ProcessInfo.processInfo.systemUptime) { AppLog.write("perf", line) } }
        // Downloaded episodes stay listed (from the iPhone) and play without the Mac.
        guard await app.reachMac() else { if episodesError != Self.episodesOffline { episodesError = Self.episodesOffline }; return }
        perf.mark("reach", at: ProcessInfo.processInfo.systemUptime)
        await sendVideoPositions()
        perf.mark("positions", at: ProcessInfo.processInfo.systemUptime)
        do {
            let requested = Date().timeIntervalSince1970
            let answer = try await connection().sendIfChanged("/api/v2/media/anime", etag: live ? animeListTag : nil, as: AnimeListResponse.self)
            perf.mark("list", at: ProcessInfo.processInfo.systemUptime)
            guard let list = answer.value else {
                // Unchanged on the Mac: nothing to apply (watched downloads past their delay still go).
                if episodesError != nil { episodesError = nil }
                deleteWatchedEpisodes()
                return
            }
            animeListTag = answer.etag
            // Published only when changed: every assignment redrew the Anime tab.
            if animeGroups != list.items { animeGroups = list.items }
            if macHome != list.home { macHome = list.home }
            animeListRequestedAt = requested
            // Watches the Mac had before this list was asked for are in it now.
            let kept = watchedHere.filter { $0.value.newer(than: requested) }
            if kept.count != watchedHere.count { watchedHere = kept }
            // The Mac's agent.delete_after_watched_hours (absent: an older Mac, nothing deleted).
            if let hours = list.deleteWatchedAfterHours, hours.isFinite {
                UserDefaults.standard.set(max(0, hours), forKey: Self.macDeleteWatchedKey)
            } else {
                UserDefaults.standard.removeObject(forKey: Self.macDeleteWatchedKey)
            }
            deleteWatchedEpisodes()
            let all = animeGroups.flatMap(\.episodes)
            if episodes != all { episodes = all }
            adoptMacPlaces(episodes)
            adoptMacWatches()
            // What another device watched goes now too (it was kept for good: its place here is at the start).
            deleteWatchedEpisodes()
            adoptMacPlayback(episodes)
            if episodesError != nil { episodesError = nil }
            perf.mark("apply", at: ProcessInfo.processInfo.systemUptime)
            // Corrected subtitles of downloaded episodes come with the list; their words' meanings
            // (many requests, the cue files read) only with a full refresh.
            await refreshDownloadedSubtitles(words: !live)
            perf.mark("subtitles", at: ProcessInfo.processInfo.systemUptime)
        } catch { episodesError = error.localizedDescription }
    }

    /// The Mac's place of a downloaded episode with nothing unsent here (watched further on the
    /// Mac): it opens there, offline too. It opened at the phone's older place, and closing it
    /// sent that place back as the newest.
    private func adoptMacPlaces(_ items: [EpisodeItem]) {
        for item in items where item.position.isFinite {
            guard var local = localEpisodes[item.id], !local.positionDirty, !isOtherMac(local) else { continue }
            let mac = max(0, Int64((item.position * 1000).rounded()))
            guard mac != local.positionMs else { continue }
            local.positionMs = mac
            localEpisodes[item.id] = local
            try? JSONEncoder().encode(local).write(to: metaURL(item.id), options: .atomic)
        }
    }

    /// Downloaded episodes the Mac counted as watched (mpv, another device): its list no longer
    /// offers them and the title's progress there reached them (MacWatchedEpisodes). The copy here
    /// is marked watched on the Mac and starts from the beginning: before, it was listed on its own
    /// with its old place, as "Continue ep 5" (the next episode to finish) on the card and the shelf.
    /// A copy with a place or a watch of this device not sent yet, or open in the player, stays.
    private func adoptMacWatches() {
        let listed = Set(animeGroups.flatMap { $0.episodes.map(\.id) })
        let now = Date().timeIntervalSince1970
        for var local in Array(localEpisodes.values) where !isOtherMac(local) {
            guard let mediaId = local.mediaId, mediaId > 0 else { continue }
            let progress = animeGroups.first { $0.mediaId == mediaId }?.progress ?? macHomeCard(mediaId)?.progress
            guard MacWatchedEpisodes.adopts(number: local.number, listed: listed.contains(local.id), progress: progress,
                                            watchedHere: local.watchedSent != nil, positionUnsent: local.positionDirty,
                                            playing: playingEpisodes.contains(local.id)) else { continue }
            local.watchedSent = true
            local.watchedAt = local.watchedAt ?? now
            local.watchedElsewhere = true
            local.positionMs = 0
            localEpisodes[local.id] = local
            try? JSONEncoder().encode(local).write(to: metaURL(local.id), options: .atomic)
            AppLog.write("video", "episode \(local.id) watched on the Mac (progress \(progress ?? 0)): marked watched here")
        }
    }

    /// The Mac's subtitle timing (set in mpv) and detected OP/ED of the listed episodes; the
    /// segments are kept with a downloaded episode for its Skip button offline.
    private func adoptMacPlayback(_ items: [EpisodeItem]) {
        for item in items {
            subtitleDelays.adopt(mac: item.subtitleDelayMs, for: item.id)
            guard let segments = item.skipSegments, var local = localEpisodes[item.id], !isOtherMac(local),
                  local.skipSegments != segments else { continue }
            local.skipSegments = segments
            localEpisodes[item.id] = local
            try? JSONEncoder().encode(local).write(to: metaURL(item.id), options: .atomic)
        }
    }

    /// Another copy of the same episode (the Mac had two files of it) downloaded here, being
    /// downloaded or waiting in "Download all": its id, nil when there is none.
    func otherCopy(of episode: EpisodeItem) -> Int? {
        var others: [(id: Int, mediaId: Int, number: Int)] = localEpisodes.values.compactMap { local in
            guard let mediaId = local.mediaId, !isOtherMac(local) else { return nil }
            return (id: local.id, mediaId: mediaId, number: local.number)
        }
        others += videoJobItems.values.map { (id: $0.id, mediaId: $0.mediaId, number: $0.number) }
        others += videoQueue.prefix(while: { $0.id != episode.id }).map { (id: $0.id, mediaId: $0.mediaId, number: $0.number) }
        return EpisodeCopies.otherCopy(of: episode, among: others)
    }

    /// The same for downloaded volumes (read further on the Mac or another device).
    private func adoptMacReadingPlaces() {
        for var record in readingDownloads.values where record.positionDirty != true && !isOtherMac(record) {
            if record.kind == "ln", let item = novels.first(where: { $0.id == record.id }),
               record.chapter != item.chapter || record.offset != item.offset {
                record.chapter = item.chapter; record.offset = item.offset
                saveReadingRecord(record)
            } else if record.kind == "manga", let item = manga.first(where: { $0.id == record.id }), record.page != item.position {
                record.page = item.position; record.readThrough = nil
                saveReadingRecord(record)
            }
        }
    }

    /// Downloaded episodes get the Mac's corrected subtitles (e.g. the right Japanese track).
    private func refreshDownloadedSubtitles(words: Bool = true) async {
        guard let client = app.macConnection else { return }
        for item in episodes {
            guard let local = localEpisodes[item.id], !isOtherMac(local), let revision = item.subtitleRevision, local.subtitleRevision != revision else { continue }
            do {
                let track = try await client.media("episodes/\(item.id)/subtitles", as: SubtitleTrack.self)
                // The record as it is now: the copy taken before the request wrote back an older
                // place (played meanwhile) and brought back an episode deleted meanwhile.
                guard localEpisodes[item.id] != nil else { continue }
                // Encoded and written off the main thread (MBs of JSON).
                try await writeSubtitlesNow(item.id, track: track)
                guard var current = localEpisodes[item.id] else { continue }
                current.subtitleRevision = revision
                current.mediaId = item.mediaId
                localEpisodes[item.id] = current
                try? JSONEncoder().encode(current).write(to: metaURL(item.id), options: .atomic)
                wordsChecked.remove(item.id)
                await saveWithLiveStates(item.id, track: track)
                AppLog.write("video", "subtitles refreshed for episode \(item.id)")
            } catch { continue }
        }
        guard words else { return }
        for id in localEpisodes.keys.sorted() where !wordsChecked.contains(id) {
            // The cue file (MBs) is read off the main thread; only the words are needed here.
            let url = cuesURL(id)
            guard let tokens = await Task.detached(priority: .utility, operation: { MediaModel.cueTokens(url) }).value else { continue }
            if await rememberWords(tokens) { wordsChecked.insert(id) }
        }
    }

    /// The words of a downloaded episode's subtitles (nil without a readable cue file).
    nonisolated static func cueTokens(_ url: URL) -> [StudyToken]? {
        guard let data = try? Data(contentsOf: url), let track = try? JSONDecoder().decode(SubtitleTrack.self, from: data) else { return nil }
        return track.cues.flatMap(\.tokens)
    }

    // MARK: Offline word data

    /// Meanings of words in downloaded subtitles, asked from the Mac ahead of time (only
    /// missing ones) so a tap shows them without the Mac. False when the Mac did not answer.
    @discardableResult
    func rememberWords(_ tokens: [StudyToken]) async -> Bool {
        guard let client = app.macConnection else { return false }
        await preloadOfflineWords()
        // Words the Mac had no meaning for lately are not asked again on every connect.
        let pairs = offlineWords.missing(tokens)
        guard !pairs.isEmpty else { return true }
        struct Request: Codable { let pairs: [[Int]] }
        var saved = 0, asked = 0
        defer {
            if asked > 0 { saveOfflineWords() }
            if saved > 0 { AppLog.write("study", "\(saved) of \(pairs.count) missing words saved for offline use") }
        }
        for start in stride(from: 0, to: pairs.count, by: 200) {
            let chunk = Array(pairs[start..<min(pairs.count, start + 200)])
            guard let value = try? await client.media("study/words", post: Request(pairs: chunk), as: ItemsResponse<WordDetail>.self) else { return false }
            offlineWords.remember(value.items)
            offlineWords.markAsked(chunk)
            saved += value.items.count; asked += chunk.count
        }
        return true
    }

    /// The saved word data (MBs of JSON) is read off the main thread once, after launch: it was
    /// decoded on the main thread at the first word lookup or connect.
    func preloadOfflineWords() async {
        guard !offlineWordsLoaded else { return }
        let url = offlineWordsURL
        let started = ProcessInfo.processInfo.systemUptime
        let value = await Task.detached(priority: .utility) { OfflineWords.load(url) }.value
        // Read on the main thread meanwhile (a word tapped at once): that copy stands.
        guard !offlineWordsLoaded else { return }
        offlineWords = value
        offlineWordsLoaded = true
        let seconds = ProcessInfo.processInfo.systemUptime - started
        if seconds >= 1 { AppLog.write("perf", String(format: "saved words read in %.1f s (%d)", seconds, value.entries.count)) }
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

    /// The saved copy of the period shows at once (never another period's numbers), then the
    /// Mac's answer replaces it quietly; while the Mac answers with its kept copy marked stale
    /// (rebuilding it), it is asked again a few times.
    func refreshStats(period: String) async {
        statsPeriodWanted = period
        let key = "stats-" + period, server = boundServer
        if stats?.period != period {
            let saved = screenCache.load(key, server: server, as: StatsSummary.self)
            stats = saved?.value; statsSavedAt = saved?.savedAt
        }
        loadingStats = true
        defer { if statsPeriodWanted == period { loadingStats = false } }
        guard await app.reachMac() else { statsError = PudgeModel.unavailable("statistics come from the Mac"); return }
        var attempt = 0
        while true {
            do {
                let value = try await connection().media("stats?period=" + period, as: StatsSummary.self)
                guard statsPeriodWanted == period, !Task.isCancelled else { return }
                stats = value; statsSavedAt = nil; statsError = nil
                screenCache.save(value, key: key, server: server)
                guard value.stale == true, let delay = ScreenCache.staleRetryDelay(attempt: attempt) else { return }
                attempt += 1
                try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            } catch {
                let cancelled = error is CancellationError || (error as? URLError)?.code == .cancelled
                if !cancelled && statsPeriodWanted == period { statsError = error.localizedDescription }
                return
            }
        }
    }

    func refreshPlanning() async {
        let server = boundServer
        if planning == nil, let saved = screenCache.load("planning", server: server, as: PlanningSummary.self) {
            planning = saved.value; planningFromCache = true
        }
        guard await app.reachMac() else { planningError = Self.episodesOffline; return }
        do {
            let value = try await connection().media("planning", as: PlanningSummary.self)
            planning = value; planningFromCache = false; planningError = nil
            screenCache.save(value, key: "planning", server: server)
        } catch { planningError = error.localizedDescription }
    }

    /// A picture (manga page, illustration, cover) read and decoded off the main thread, scaled
    /// down to DownsampledImage.screenLimit, kept in the cost-limited cache.
    func image(_ path: String) async -> UIImage? {
        let key = path as NSString
        if let cached = images.object(forKey: key) { return cached }
        let limit = DownsampledImage.screenLimit()
        let local = localReadingImage(path)
        let folder = app.store.directory.appendingPathComponent("image-cache", isDirectory: true)
        let file = folder.appendingPathComponent(path.replacingOccurrences(of: "/", with: "_"))
        // Covers and illustrations (content-addressed on the Mac) are kept on disk.
        let keep = path.hasPrefix("cover/") || path.hasPrefix("ln-image/")
        let saved = await Task.detached(priority: .userInitiated) { () -> UIImage? in
            if let local, let image = DownsampledImage.load(local, maxPixels: limit) { return image }
            return keep ? DownsampledImage.load(file, maxPixels: limit) : nil
        }.value
        if let saved {
            images.setObject(saved, forKey: key, cost: DownsampledImage.cost(saved)); return saved
        }
        // Away from the Mac (or without Wi-Fi) a missing cover is not waited for on every row.
        guard !app.macOffline, let data = try? await connection().bytes("/api/v2/media/" + path) else { return nil }
        let fetched = await Task.detached(priority: .userInitiated) { () -> UIImage? in
            guard let image = DownsampledImage.load(data, maxPixels: limit) else { return nil }
            if keep {
                try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                try? data.write(to: file, options: .atomic)
            }
            return image
        }.value
        guard let fetched else { return nil }
        images.setObject(fetched, forKey: key, cost: DownsampledImage.cost(fetched))
        return fetched
    }

    // MARK: Light novels

    func novel(_ id: Int) async throws -> LightNovelDetail {
        // A volume of a previous Mac opens from the device (this Mac's volume of that number is another one).
        if isOtherMacVolume("ln", id), let saved = savedNovel(id)?.book { return saved }
        // A downloaded volume opens from the iPhone when the Mac does not answer.
        if let saved = savedNovel(id)?.book, !(await app.reachMac()) { return saved }
        do { return try await connection().media("ln/\(id)", as: LightNovelDetail.self) }
        catch { if let saved = savedNovel(id)?.book { return saved }; throw error }
    }

    /// Where a volume opens: the place read on this iPhone while the Mac did not know it yet.
    func startPosition(_ id: Int, _ detail: LightNovelDetail) -> (chapter: Int, offset: Double) {
        if let record = readingDownloads["ln:\(id)"], let chapter = record.chapter, record.positionDirty == true || app.macOffline || isOtherMac(record) {
            return (chapter, record.offset ?? 0)
        }
        return (detail.chapter, detail.offset)
    }

    /// Chapter text; while the Mac is still parsing words, polls until tokens arrive.
    func chapter(_ book: Int, index: Int, update: (ChapterText) -> Void) async throws {
        // Downloaded chapters open from the iPhone (fresh word colours follow when the Mac answers).
        if var saved = await savedChapter(book, index: index) {
            // Words graded since the download keep the colours of the last update.
            let newer = offlineWords.states(newerThan: readingDownloads["ln:\(book)"]?.savedAt ?? 0)
            if !newer.isEmpty { for row in saved.paragraphs.indices { saved.paragraphs[row].tokens = saved.paragraphs[row].tokens.updating(newer) } }
            update(saved)
            if app.macOffline || isOtherMacVolume("ln", book) || saved.paragraphs.contains(where: { !$0.tokens.isEmpty }) { return }
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

    /// `paragraph`: the reader paragraph at the top (the Mac moves the read marker by it); the
    /// answer is the marker as the Mac has it now (nil offline or from older Macs).
    @discardableResult
    func saveNovelPosition(_ book: Int, chapter: Int, offset: Double, finished: Bool? = nil, paragraph: Int? = nil) async -> ReadMarker? {
        let key = "ln:\(book)"
        if var record = readingDownloads[key] {
            record.chapter = chapter; record.offset = offset; record.positionDirty = true; record.positionAt = Date().timeIntervalSince1970
            saveReadingRecord(record)
            if isOtherMac(record) { return nil }  // its place stays here
        }
        guard !app.macOffline else { return nil }
        guard let answer = try? await connection().media("ln/\(book)/position",
            post: NovelPositionRequest(chapter: chapter, offset: offset, finished: finished, paragraph: paragraph), as: ReadMarkerResponse.self) else { return nil }
        if var record = readingDownloads[key], record.chapter == chapter, record.offset == offset {
            record.positionDirty = false; saveReadingRecord(record)
        }
        if let left = pendingReading[key] { forgetPendingReading(left) }  // older than this place
        return answer.marker
    }

    /// "Read up to here" (`kind` "manual") or the sentence the audio reads ("auto"); nil when the
    /// Mac does not answer: the marker then waits in `pendingMarkers` and goes with the queued places.
    /// Only manual and queued markers carry a `markerId` (the Mac keeps those ids 30 days).
    func saveReadMarker(_ book: Int, chapter: Int, paragraph: Int, utf16: Int, kind: String) async -> ReadMarker? {
        if let record = readingDownloads["ln:\(book)"], isOtherMac(record) { return nil }
        let request = ReadMarkerRequest(chapter: chapter, paragraph: paragraph, paragraphOffset: utf16, kind: kind,
                                        markerId: kind == "manual" ? UUID().uuidString : nil)
        guard !app.macOffline, let client = app.macConnection else { queueMarker(book, request); return nil }
        // Markers set earlier go first: an older one sent after this one could still win (further ahead).
        await sendReadMarkers(client)
        guard pendingMarkers.isEmpty else { queueMarker(book, request); return nil }
        switch await postMarker(client, book, request) {
        case .sent(let marker): return marker
        case .later: queueMarker(book, request); return nil
        case .noRoute: markersUnsupported(); return nil
        case .refused: return nil  // the volume is gone on the Mac, or the request is refused for good
        }
    }

    /// How the Mac took a marker: sent, later (not reachable, or busy: HTTP 503), refused for good,
    /// or no marker route at all (an older Mac: HTTP 404).
    private enum MarkerSend { case sent(ReadMarker?), later, refused, noRoute }

    private func postMarker(_ client: MacConnection, _ book: Int, _ request: ReadMarkerRequest) async -> MarkerSend {
        do {
            let route = "/api/v2/media/ln/\(book)/marker"
            let (body, response) = try await client.data(for: client.request(route, method: "POST", data: JSONEncoder().encode(request)))
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if (200..<300).contains(status) {
                UserDefaults.standard.removeObject(forKey: Self.noMarkerRouteKey)
                return .sent((try? JSONDecoder().decode(ReadMarkerResponse.self, from: body))?.marker)
            }
            if status == 503 { return .later }
            return status == 404 ? .noRoute : .refused
        } catch {
            return unreachable(error) ? .later : .refused
        }
    }

    /// The Mac this device is bound to answered the marker route with 404 (an older Pudge): its
    /// queue is dropped and nothing more is queued for it until a marker reaches it.
    private static let noMarkerRouteKey = "pudge.readMarkersUnsupportedMac"
    private func markersUnsupported() {
        UserDefaults.standard.set(boundServer ?? "", forKey: Self.noMarkerRouteKey)
        pendingMarkers.removeAll()
        savePendingMarkers()
    }

    private static func loadPendingMarkers(_ url: URL) -> [PendingReadMarker] {
        guard let data = try? Data(contentsOf: url) else { return [] }
        return (try? JSONDecoder().decode([PendingReadMarker].self, from: data)) ?? []
    }

    private func savePendingMarkers() {
        try? JSONEncoder().encode(pendingMarkers).write(to: pendingMarkersURL, options: .atomic)
    }

    /// Kept until the Mac answers: an automatic marker replaces the volume's earlier automatic one.
    private func queueMarker(_ book: Int, _ request: ReadMarkerRequest) {
        if let unsupported = UserDefaults.standard.string(forKey: Self.noMarkerRouteKey), unsupported == (boundServer ?? "") { return }
        var request = request
        let id = request.markerId ?? UUID().uuidString
        request.markerId = id
        if request.kind == "auto" { pendingMarkers.removeAll { $0.book == book && $0.request.kind == "auto" } }
        pendingMarkers.append(PendingReadMarker(markerId: id, book: book, request: request, serverId: boundServer))
        if pendingMarkers.count > 500 { pendingMarkers.removeFirst(pendingMarkers.count - 500) }
        savePendingMarkers()
    }

    /// Queued markers in the order they were set, each with its own id (a resend the Mac already
    /// applied is answered as before). Stops while the Mac is not reachable or busy (HTTP 503); one
    /// refused for any other reason (the volume deleted on the Mac: 422), one of another Mac and one
    /// older than 30 days (the Mac keeps ids that long) are dropped; an older Mac without the
    /// route (404) drops them all.
    func sendReadMarkers(_ client: MacConnection) async {
        for item in pendingMarkers {
            let stale = Date().timeIntervalSince1970 - (item.request.observedAt ?? 0) > 30 * 86_400
            if !isOtherMac(item.serverId) && !stale {
                var request = item.request
                request.sentAt = Date().timeIntervalSince1970
                switch await postMarker(client, item.book, request) {
                case .later: return
                case .noRoute: markersUnsupported(); return
                case .sent, .refused: break
                }
            }
            pendingMarkers.removeAll { $0.markerId == item.markerId }
            savePendingMarkers()
        }
    }

    // MARK: Manga

    func mangaText(_ book: Int, page: Int) async -> MangaPageText? {
        if readingDownloads["manga:\(book)"] != nil {
            let file = readingURL("manga", book).appendingPathComponent("text-\(page).json")
            let saved = await Task.detached(priority: .userInitiated) { () -> MangaPageText? in
                guard let data = try? Data(contentsOf: file) else { return nil }
                return try? JSONDecoder().decode(MangaPageText.self, from: data)
            }.value
            if let saved { return saved }
        }
        return try? await connection().media("manga/\(book)/pages/\(page)/text", as: MangaPageText.self)
    }

    /// The pages of a volume that are one wide picture (double-page spreads, shown alone in the
    /// two-page view), its usual page shape and its "Shift pairs by one page". A downloaded
    /// volume's pages are its own (package.json, else the files' headers, off the main thread);
    /// the shift comes from the Mac when it answers. nil: nothing known (offline, not downloaded).
    func mangaSpreadPages(_ book: Int) async -> MangaSpreadPages? {
        let key = "manga:\(book)"
        // A change made here while this asks (setMangaPairShift/setMangaPageAlign) is newer than
        // whatever the Mac answers now: then its shift and alignment setting are not taken.
        let changes = spreadSettingChanges[key, default: 0]
        var known: MangaSpreadPages?
        if let package = savedManga(book) {
            let folder = readingURL("manga", book)
            let files: [URL?] = package.pages.map { ReadingPackage.isSafe($0) ? folder.appendingPathComponent($0) : nil }
            let shapes = await Task.detached(priority: .userInitiated) { mangaPageShapes(files) }.value
            known = MangaSpreadPages(wide: package.widePages ?? shapes.wide, aspect: shapes.aspect,
                                     shift: pairShifts[key] ?? package.pairShift,
                                     align: pageAligns[key] ?? package.pageAlign, alignment: package.alignment)
            if isOtherMacVolume("manga", book) { return known }  // that Mac's numbers
        }
        guard !app.macOffline, let client = try? connection() else { return known }
        var answer: MangaSpreadPages?
        if pairShiftsUnsent.contains(key), let shift = pairShifts[key] {
            // Changed here without the Mac: this change goes first.
            answer = try? await client.media("manga/\(book)/spreads", post: MangaPairShiftRequest(shift: shift), as: MangaSpreadPages.self)
            // Sent, unless it was changed again meanwhile (that one is still to send).
            if answer != nil, pairShifts[key] == shift { pairShiftsUnsent.remove(key) }
        }
        if pageAlignsUnsent.contains(key), let align = pageAligns[key] {
            switch await postPageAlign(client, book, align) {
            case .sent(let sent):
                if let sent { answer = sent }
                if pageAligns[key] == align { pageAlignsUnsent.remove(key) }
            case .refused:
                pageAlignsUnsent.remove(key)  // an older Mac: the setting stays on this device
            case .later:
                break
            }
        }
        if answer == nil { answer = try? await client.media("manga/\(book)/spreads", as: MangaSpreadPages.self) }
        guard let answer else { return known }
        let current = spreadSettingChanges[key, default: 0] == changes
        if current, let shift = answer.shift, !pairShiftsUnsent.contains(key) { pairShifts[key] = shift }
        if current, let align = answer.align, !pageAlignsUnsent.contains(key) { pageAligns[key] = align }
        // A change made here and not yet on the Mac (or made while this asked) stays as it is here.
        let shift = !current || pairShiftsUnsent.contains(key) ? pairShifts[key] : (answer.shift ?? known?.shift ?? pairShifts[key])
        let align = !current || pageAlignsUnsent.contains(key) ? pageAligns[key] : (answer.align ?? known?.align ?? pageAligns[key])
        // A downloaded volume keeps its own pages (the Mac's may have changed since): its own
        // alignment once the Mac had finished it, else what the Mac has now.
        if let known {
            let complete = known.alignment.map { ($0.pending ?? 0) == 0 } ?? false
            let alignment = complete ? known.alignment : (answer.alignment ?? known.alignment)
            return MangaSpreadPages(wide: known.wide, aspect: known.aspect, shift: shift, align: align, alignment: alignment)
        }
        return MangaSpreadPages(wide: answer.wide, aspect: answer.aspect, shift: shift, align: align, alignment: answer.alignment)
    }

    /// "Shift pairs by one page" for a volume, as last known on this device.
    func mangaPairShift(_ book: Int) -> Bool {
        pairShifts["manga:\(book)"] ?? savedManga(book)?.pairShift ?? false
    }

    /// Changes "Shift pairs by one page" here and on the Mac (later, when it does not answer now).
    func setMangaPairShift(_ book: Int, _ shift: Bool) async {
        let key = "manga:\(book)"
        spreadSettingChanges[key, default: 0] += 1
        pairShifts[key] = shift
        guard !isOtherMacVolume("manga", book) else { return }  // stays on this device
        pairShiftsUnsent.insert(key)
        guard !app.macOffline,
              (try? await connection().media("manga/\(book)/spreads", post: MangaPairShiftRequest(shift: shift), as: MangaSpreadPages.self)) != nil
        else { return }
        if pairShifts[key] == shift { pairShiftsUnsent.remove(key) }
    }

    /// "Align scanned pages" for a volume, as last known on this device (on unless turned off).
    func mangaPageAlign(_ book: Int) -> Bool {
        pageAligns["manga:\(book)"] ?? savedManga(book)?.pageAlign ?? true
    }

    /// Changes "Align scanned pages" here and on the Mac (later, when it does not answer now).
    func setMangaPageAlign(_ book: Int, _ align: Bool) async {
        let key = "manga:\(book)"
        spreadSettingChanges[key, default: 0] += 1
        pageAligns[key] = align
        guard !isOtherMacVolume("manga", book) else { return }  // stays on this device
        pageAlignsUnsent.insert(key)
        guard !app.macOffline, let client = try? connection() else { return }
        switch await postPageAlign(client, book, align) {
        case .sent: if pageAligns[key] == align { pageAlignsUnsent.remove(key) }
        case .refused: pageAlignsUnsent.remove(key)  // an older Mac: the setting stays on this device
        case .later: break
        }
    }

    /// Sending "Align scanned pages" to the Mac: sent (with the Mac's answer), to send later (no
    /// answer now), or refused: a Mac before the setting answers 400/422 (it wants `shift`), or
    /// the volume is gone; it is not sent again.
    private enum PageAlignSend { case sent(MangaSpreadPages?), later, refused }

    private func postPageAlign(_ client: MacConnection, _ book: Int, _ align: Bool) async -> PageAlignSend {
        do {
            let route = "/api/v2/media/manga/\(book)/spreads"
            let data = try JSONEncoder().encode(MangaPageAlignRequest(align: align))
            let (body, response) = try await client.data(for: client.request(route, method: "POST", data: data))
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if (200..<300).contains(status) { return .sent(try? JSONDecoder().decode(MangaSpreadPages.self, from: body)) }
            return [400, 404, 422].contains(status) ? .refused : .later
        } catch {
            return .later
        }
    }

    /// Changes of the shift or "Align scanned pages" made here, per volume (see mangaSpreadPages).
    private var spreadSettingChanges: [String: Int] = [:]

    private var pageAligns: [String: Bool] {
        get { (UserDefaults.standard.dictionary(forKey: "pudge.mangaPageAlign") as? [String: Bool]) ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: "pudge.mangaPageAlign") }
    }
    /// Volumes whose "Align scanned pages" was changed here and not yet sent to the Mac.
    private var pageAlignsUnsent: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: "pudge.mangaPageAlignUnsent") ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: "pudge.mangaPageAlignUnsent") }
    }

    private var pairShifts: [String: Bool] {
        get { (UserDefaults.standard.dictionary(forKey: "pudge.mangaPairShift") as? [String: Bool]) ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: "pudge.mangaPairShift") }
    }
    /// Volumes whose shift was changed here and not yet sent to the Mac.
    private var pairShiftsUnsent: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: "pudge.mangaPairShiftUnsent") ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: "pudge.mangaPairShiftUnsent") }
    }

    /// The page a volume opens on (read further on this iPhone than the Mac knows).
    func startPage(_ book: MangaItem) -> Int {
        if let record = readingDownloads["manga:\(book.id)"], let page = record.page, record.positionDirty == true || app.macOffline || isOtherMac(record) { return page }
        return book.position
    }

    /// `readThrough`: two pages on screen, the second one (`page + 1`) was read too.
    func saveMangaPosition(_ book: Int, page: Int, readThrough: Int? = nil) async {
        let key = "manga:\(book)"
        if var record = readingDownloads[key] {
            record.page = page; record.readThrough = readThrough; record.positionDirty = true; record.positionAt = Date().timeIntervalSince1970
            saveReadingRecord(record)
            if isOtherMac(record) { return }  // its place stays here
        }
        guard !app.macOffline else { return }
        guard (try? await connection().media("manga/\(book)/position", post: MangaPositionRequest(page: page, read: true, readThrough: readThrough), as: OkResponse.self)) != nil else { return }
        if var record = readingDownloads[key], record.page == page { record.positionDirty = false; saveReadingRecord(record) }
        if let left = pendingReading[key] { forgetPendingReading(left) }  // older than this place
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
        var result: [String: ReadingDownload] = [:], pending: [String: ReadingDownload] = [:]
        let manager = FileManager.default
        let bound = boundServer
        let folders = (try? manager.contentsOfDirectory(at: readingFolder, includingPropertiesForKeys: nil)) ?? []
        for folder in folders {
            if folder.pathExtension == "part" && readingJobs.isEmpty { try? manager.removeItem(at: folder); continue }  // interrupted
            // Only when nothing downloads: another volume's unpacking was removed under it.
            if folder.lastPathComponent.hasSuffix(".unpacking") { if readingJobs.isEmpty { try? manager.removeItem(at: folder) }; continue }
            if folder.lastPathComponent.hasSuffix(".pending.json") {
                if let data = try? Data(contentsOf: folder), let record = try? JSONDecoder().decode(ReadingDownload.self, from: data) { pending[record.key] = record }
                continue
            }
            let file = folder.appendingPathComponent("download.json")
            guard let data = try? Data(contentsOf: file), var record = try? JSONDecoder().decode(ReadingDownload.self, from: data) else { continue }
            // Renumbered (below) but stopped before its record was written: the folder's name tells.
            let name = folder.lastPathComponent
            if name.hasPrefix(record.kind + "-"), let id = Int(name.dropFirst(record.kind.count + 1)), id != record.id {
                try? ReadingPackage.renumber(folder, id: id)
                record.id = id
                try? JSONEncoder().encode(record).write(to: file, options: .atomic)
            }
            // Downloaded before the Mac was kept with it: it belongs to the Mac bound now (once).
            if record.serverId == nil, let bound {
                record.serverId = bound
                try? JSONEncoder().encode(record).write(to: file, options: .atomic)
            }
            result[record.key] = record
        }
        // Volumes of a previous Mac leave this Mac's numbers: kept and readable, never synced.
        for kind in ["ln", "manga"] {
            let owners = result.values.filter { $0.kind == kind }.map { (id: $0.id, serverId: $0.serverId) }
            for (old, new) in DownloadOwner.localIds(owners, bound: bound) {
                guard var record = result.removeValue(forKey: "\(kind):\(old)") else { continue }
                do { try manager.moveItem(at: readingURL(kind, old), to: readingURL(kind, new)) } catch {
                    result[record.key] = record
                    AppLog.write("reading", "\(kind) \(old) of a previous Mac not renumbered: \(error.localizedDescription)")
                    continue
                }
                try? ReadingPackage.renumber(readingURL(kind, new), id: new)
                record.id = new
                try? JSONEncoder().encode(record).write(to: readingURL(kind, new).appendingPathComponent("download.json"), options: .atomic)
                result[record.key] = record
                AppLog.write("reading", "\(kind) \(old) of a previous Mac kept as \(new), not synced")
            }
        }
        readingDownloads = result
        pendingReading = pending
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

    /// A downloaded chapter, read and decoded off the main thread (its words make it large).
    func savedChapter(_ book: Int, index: Int) async -> ChapterText? {
        guard isDownloaded("ln", book) else { return nil }
        let file = readingURL("ln", book).appendingPathComponent("chapter-\(index).json")
        return await Task.detached(priority: .userInitiated) { () -> ChapterText? in
            guard let data = try? Data(contentsOf: file) else { return nil }
            return try? JSONDecoder().decode(ChapterText.self, from: data)
        }.value
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

    /// The downloaded manga volumes' parsed texts, read once off the main thread.
    private func savedParse(_ text: String) async -> ParsedText? {
        if savedParses == nil {
            let files = parsedFiles()
            let all = await Task.detached(priority: .userInitiated) { () -> [String: ParsedText] in
                var all: [String: ParsedText] = [:]
                for file in files {
                    guard let data = try? Data(contentsOf: file), let value = try? JSONDecoder().decode(ParsedRegions.self, from: data) else { continue }
                    for item in value.items { all[item.text] = ParsedText(paragraphs: item.paragraphs) }
                }
                return all
            }.value
            // Kept unless the downloaded volumes changed meanwhile.
            if savedParses == nil, parsedFiles() == files { savedParses = all }
            return all[text]
        }
        return savedParses?[text]
    }
    private func parsedFiles() -> [URL] {
        readingDownloads.values.filter { $0.kind == "manga" }.map { readingURL("manga", $0.id).appendingPathComponent("parsed.json") }
            .sorted { $0.path < $1.path }
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
        let key = "\(kind):\(id)"
        cancelReading(kind, id)
        // A place read here that the Mac has not got yet stays (without the volume) and is sent
        // when the Mac answers: removing the download lost it. Not for a volume the Mac deleted.
        var unsent: ReadingDownload?
        if let record = readingDownloads[key], record.positionDirty == true, record.deletedOnMacAt == nil, !isOtherMac(record) {
            unsent = record
            try? JSONEncoder().encode(record).write(to: pendingReadingURL(kind, id), options: .atomic)
        }
        try? FileManager.default.removeItem(at: readingURL(kind, id))
        readingRemoved.insert(key)
        loadReadingDownloads()
        AppLog.write("reading", "\(kind) \(id) removed from this device" + (unsent == nil ? "" : "; its place waits for the Mac"))
        if unsent != nil { Task { await sendReadingPositions() } }
    }

    private func pendingReadingURL(_ kind: String, _ id: Int) -> URL { readingFolder.appendingPathComponent("\(kind)-\(id).pending.json") }
    private func forgetPendingReading(_ record: ReadingDownload) {
        pendingReading[record.key] = nil
        try? FileManager.default.removeItem(at: pendingReadingURL(record.kind, record.id))
    }
    /// The place read here that a new download of the volume keeps (also one waiting after removal).
    private func placeToKeep(_ key: String) -> ReadingDownload? {
        if let record = readingDownloads[key], !isOtherMac(record) { return record }
        return pendingReading[key]
    }

    /// The Mac packs the volume (chapters with words, illustrations; manga pages and text
    /// boxes); one verified file comes over and is unpacked here.
    @discardableResult
    func downloadReading(_ kind: String, id: Int, title: String, quiet: Bool = false) async -> Bool {
        let key = "\(kind):\(id)"
        guard readingJobs[key] == nil else { return false }
        let updating = readingDownloads[key] != nil
        readingCancelled.remove(key)
        var removed = readingRemoved; removed.remove(key); readingRemoved = removed
        readingJobs[key] = VideoJob(phase: "Preparing on the Mac…", fraction: 0)
        let partial = readingFolder.appendingPathComponent("\(kind)-\(id).part")
        defer { readingJobs[key] = nil; readingTasks[key] = nil }
        func checkCancelled() throws { if readingCancelled.contains(key) { throw CancellationError() } }
        do {
            let client = try connection()
            let server = client.mac.serverId
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
                               sha256: package.sha256, sizeBytes: package.sizeBytes,
                               started: { [weak self] task in self?.readingTasks[key] = task }) { [weak self] value in
                if self?.readingJobs[key] != nil { self?.readingJobs[key] = VideoJob(phase: "Downloading…", fraction: value) }
            }
            try checkCancelled()
            readingJobs[key] = VideoJob(phase: "Verifying…", fraction: 1)
            let target = readingURL(kind, id), sha = package.sha256, size = package.sizeBytes
            let revision = state.revision, pagesRevision = state.pagesRevision
            // The record goes into the new copy before it replaces the old one: a kill right after
            // the swap left a volume without its record (never listed, never removed).
            let early = placeToKeep(key)
            try await Task.detached(priority: .utility) {
                try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size)
                try ReadingPackage.unpack(partial, into: target) { unpacked in
                    let staged = MediaModel.packageRecord(unpacked, kind: kind, id: id, title: title, revision: revision,
                                                          pagesRevision: pagesRevision, sizeBytes: size, previous: early, serverId: server)
                    try JSONEncoder().encode(staged).write(to: unpacked.appendingPathComponent("download.json"), options: .atomic)
                }
            }.value
            Self.removePartial(partial)
            // An update keeps the place read on this iPhone, also while it downloaded: taken now,
            // not when the download started (the place read meanwhile was written over).
            let record = Self.packageRecord(target, kind: kind, id: id, title: title, revision: revision,
                                            pagesRevision: pagesRevision, sizeBytes: size, previous: placeToKeep(key), serverId: server)
            try JSONEncoder().encode(record).write(to: target.appendingPathComponent("download.json"), options: .atomic)
            if let left = pendingReading[key] { forgetPendingReading(left) }  // its place came along
            loadReadingDownloads()
            AppLog.write("reading", "\(updating ? "updated" : "downloaded") \(kind) \(id) \(size) bytes")
            readingWordsChecked.remove(key)
            Task {
                if await rememberWords(await readingTokens(record)) { readingWordsChecked.insert(key) }
            }
            return true
        } catch {
            Self.removePartial(partial)
            AppLog.write("reading", "\(kind) \(id) not downloaded: \(error.localizedDescription)")
            if !readingCancelled.contains(key) && !quiet { app.message = error.localizedDescription }
            return false
        }
    }

    /// The record of a volume unpacked into `folder`; the place read on this device (`previous`) stays.
    private nonisolated static func packageRecord(_ folder: URL, kind: String, id: Int, title: String, revision: String, pagesRevision: String?,
                                                  sizeBytes size: Int64, previous: ReadingDownload?, serverId: String) -> ReadingDownload {
        struct Described: Decodable { let title: String }
        let described = (try? Data(contentsOf: folder.appendingPathComponent("package.json"))).flatMap { try? JSONDecoder().decode(Described.self, from: $0) }
        let manga = kind == "manga" ? (try? Data(contentsOf: folder.appendingPathComponent("package.json"))).flatMap { try? JSONDecoder().decode(MangaPackage.self, from: $0) } : nil
        var record = ReadingDownload(kind: kind, id: id, title: described?.title ?? title, revision: revision,
                                     sizeBytes: size, savedAt: Date().timeIntervalSince1970,
                                     pagesRevision: manga?.pagesRevision ?? pagesRevision,
                                     ocrRevision: manga?.ocr == true ? manga?.ocrRevision : nil, ocr: manga.map { $0.ocr == true })
        record.chapter = previous?.chapter; record.offset = previous?.offset; record.page = previous?.page
        record.readThrough = previous?.readThrough; record.positionDirty = previous?.positionDirty
        record.positionAt = previous?.positionAt; record.openedAt = previous?.openedAt
        record.serverId = serverId
        return record
    }

    // MARK: Manga OCR on request ("Get OCR")

    /// The volume has no complete OCR on the iPhone (downloaded) or on the Mac (listed).
    func needsOCR(_ book: MangaItem) -> Bool {
        if let record = readingDownloads["manga:\(book.id)"] { return record.ocr != true && !isOtherMac(record) }
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
        defer { Self.removePartial(partial); try? FileManager.default.removeItem(at: unpacked) }
        ocrJobs[id] = VideoJob(phase: "Downloading the OCR…", fraction: 0)
        try await transfer("/api/v2/resources/\(package.resourceId)/content?sha256=\(package.sha256)", client: client, to: partial,
                           sha256: package.sha256, sizeBytes: package.sizeBytes, started: { _ in }) { [weak self] value in
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
        // Volumes of a previous Mac are not asked about: this Mac's volume of that number is another one.
        let records = readingDownloads.values.filter { !isOtherMac($0) }.sorted { $0.savedAt < $1.savedAt }
        guard !records.isEmpty, let answer = try? await client.media("downloads/check", post: Request(items: records.map { Item(kind: $0.kind, id: $0.id) }),
                                                   as: ReadingRevisions.self) else { return }
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "pudge.readingCheckedAt")
        var updated: [String] = []
        for row in answer.items {
            guard var record = readingDownloads["\(row.kind):\(row.id)"], readingJobs[record.key] == nil, !isOtherMac(record) else { continue }
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
        let expired = readingDownloads.values.filter { ReadingRevisions.removable(deletedAt: $0.deletedOnMacAt, openedAt: $0.openedAt, now: now) && !isOtherMac($0) }
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
        await sendReadMarkers(client)
        // Places of removed volumes come after the downloaded ones.
        let records = readingDownloads.values.filter { $0.positionDirty == true } + pendingReading.values.filter { $0.positionDirty == true }
        for record in records where !isOtherMac(record) {
            do {
                if record.kind == "ln", let chapter = record.chapter {
                    _ = try await client.media("ln/\(record.id)/position",
                        post: NovelPositionRequest(chapter: chapter, offset: record.offset ?? 0, finished: nil, observedAt: record.positionAt), as: OkResponse.self)
                } else if record.kind == "manga", let page = record.page {
                    _ = try await client.media("manga/\(record.id)/position", post: MangaPositionRequest(page: page, read: true, observedAt: record.positionAt, readThrough: record.readThrough), as: OkResponse.self)
                }
                if var sent = readingDownloads[record.key], sent.chapter == record.chapter, sent.offset == record.offset, sent.page == record.page {
                    sent.positionDirty = false; saveReadingRecord(sent)
                }
                if pendingReading[record.key] == record { forgetPendingReading(record) }
            } catch {
                // A removed volume's place the Mac refuses (deleted there too) waits a month at most.
                if !unreachable(error), pendingReading[record.key] == record, Date().timeIntervalSince1970 - (record.positionAt ?? 0) > 30 * 86_400 {
                    forgetPendingReading(record)
                }
                return
            }
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
            if let saved = await savedParse(text) { return saved }
            throw PudgeError.invalid("Splitting text into words needs the Mac. Words of downloaded subtitles and manga volumes still open.")
        }
        do { return try await connection().media("study/parse", post: TextRequest(text: text), as: ParsedText.self) }
        catch { if let saved = await savedParse(text) { return saved }; throw error }
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
            if result.ok { await refreshGraded(token, row: result.row) }
            return result
        } catch {
            AppLog.write("study", "\(action) \(token.wordId): no answer")
            return StudyActionResult(ok: false, outcome: "unknown",
                message: "No answer from the Mac. The word may or may not be saved — check before trying again.")
        }
    }

    /// The new state of a graded word goes to every text on screen and to the saved states
    /// (offline colours, downloaded subtitles). An older Mac sends no state: it is asked for.
    private func refreshGraded(_ token: StudyToken, row: StudyStateRow?) async {
        var rows: [StudyStateRow] = []
        if let row, row.key == token.key {
            rows = [row]
            offlineWords.remember(states: rows); saveOfflineWords()
        } else if let fresh = await liveStates([token])[token.key] {
            rows = [fresh]  // liveStates saved it
        }
        guard !rows.isEmpty else {
            AppLog.write("study", "\(token.wordId): new state unknown, colours update with the next states")
            return
        }
        gradedWords.record(rows)
        AppLog.write("study", "\(token.wordId): state \(rows[0].state) flags \(rows[0].flags) applied")
    }

    // MARK: Video on iPhone

    var videoFolder: URL { app.store.directory.appendingPathComponent("video", isDirectory: true) }
    func videoURL(_ id: Int) -> URL { videoFolder.appendingPathComponent("\(id).mp4") }
    private func cuesURL(_ id: Int) -> URL { videoFolder.appendingPathComponent("\(id).cues.json") }
    private func metaURL(_ id: Int) -> URL { videoFolder.appendingPathComponent("\(id).json") }

    func loadLocalEpisodes() {
        var result: [Int: LocalEpisode] = [:], pending: [Int: LocalEpisode] = [:]
        let manager = FileManager.default
        let bound = boundServer
        let files = (try? manager.contentsOfDirectory(at: videoFolder, includingPropertiesForKeys: nil)) ?? []
        // A download cut by a Mac that went away keeps its part: it resumes from there
        // (resumeInterruptedDownloads), also after a relaunch.
        let resumable: Set<Int> = bound.map { Self.interrupted($0) } ?? []
        for file in files where file.pathExtension == "part" && videoJobs.isEmpty
            && !resumable.contains(Int(file.deletingPathExtension().lastPathComponent) ?? -1) {
            try? manager.removeItem(at: file)  // interrupted download (app was closed)
        }
        for file in files where file.pathExtension == "json" && !file.lastPathComponent.hasSuffix(".cues.json") {
            guard let data = try? Data(contentsOf: file), var item = try? JSONDecoder().decode(LocalEpisode.self, from: data) else { continue }
            // Downloaded before the Mac was kept with it: it belongs to the Mac bound now (once).
            if item.serverId == nil, let bound {
                item.serverId = bound
                try? JSONEncoder().encode(item).write(to: file, options: .atomic)
            }
            // Watched before the time of the watch was kept: its delay starts now (once).
            if item.watchedSent != nil, item.watchedAt == nil {
                item.watchedAt = Date().timeIntervalSince1970
                try? JSONEncoder().encode(item).write(to: file, options: .atomic)
            }
            // Deleted while a "watched" mark or a place waited for the Mac: kept until sent.
            if manager.fileExists(atPath: videoURL(item.id).path) { result[item.id] = item }
            else if item.unsent && !isOtherMac(item) { pending[item.id] = item }
        }
        // Episodes of a previous Mac leave this Mac's numbers: kept and playable, never synced.
        let owners = result.values.map { (id: $0.id, serverId: $0.serverId) }
        for (old, new) in DownloadOwner.localIds(owners, bound: bound) {
            guard var item = result.removeValue(forKey: old) else { continue }
            item.id = new
            do {
                try JSONEncoder().encode(item).write(to: metaURL(new), options: .atomic)
                try manager.moveItem(at: videoURL(old), to: videoURL(new))
            } catch {
                try? manager.removeItem(at: metaURL(new))
                item.id = old; result[old] = item
                AppLog.write("video", "episode \(old) of a previous Mac not renumbered: \(error.localizedDescription)")
                continue
            }
            try? manager.moveItem(at: cuesURL(old), to: cuesURL(new))
            try? manager.removeItem(at: metaURL(old))
            result[new] = item
            AppLog.write("video", "episode \(old) of a previous Mac kept as \(new), not synced")
        }
        localEpisodes = result
        pendingEpisodes = pending
        for item in Array(result.values) + Array(pending.values) where item.watchedSent == false && watchedHere[item.id] == nil {
            noteWatched(item)
        }
    }

    /// A watch counted on this device: the title's count shows it at once (until the Mac's list does).
    private func noteWatched(_ item: LocalEpisode) {
        guard let mediaId = item.mediaId, mediaId > 0, item.number > 0, !isOtherMac(item) else { return }
        watchedHere[item.id] = LocalAnimeWatch(mediaId: mediaId, number: item.number)
        finales.counted(item)
    }

    /// Downloaded subtitles; words take the states saved after the file was written
    /// (e.g. graded later), so colours stay as fresh as the last update.
    /// The cue file (MBs of JSON) is read and decoded off the main thread, after any write of it
    /// still running; only the newer word states are taken here.
    func subtitles(_ id: Int) async -> SubtitleTrack? {
        let url = cuesURL(id), pending = subtitleWrites[id]
        let read = await Task.detached(priority: .userInitiated) { () async -> (track: SubtitleTrack, written: Double)? in
            _ = await pending?.value
            guard let data = try? Data(contentsOf: url), let track = try? JSONDecoder().decode(SubtitleTrack.self, from: data) else { return nil }
            let written = ((try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast).timeIntervalSince1970
            return (track, written)
        }.value
        guard let read else { return nil }
        let written = read.written
        let newer = offlineWords.states(newerThan: written)
        if newer.isEmpty { return read.track }
        return await Task.detached(priority: .userInitiated) { () -> SubtitleTrack in
            var track = read.track
            for index in track.cues.indices { track.cues[index].tokens = track.cues[index].tokens.updating(newer) }
            return track
        }.value
    }

    /// Keeps the live states of a downloaded episode's words with its subtitles (offline colours).
    func saveSubtitles(_ id: Int, track: SubtitleTrack) {
        guard localEpisodes[id] != nil else { return }
        writeSubtitles(id, track: track)
    }

    /// Writes of each episode's cue file, encoded (MBs of JSON) and written off the main thread
    /// one after another: the last save is the one kept.
    private var subtitleWrites: [Int: Task<Bool, Never>] = [:]
    @discardableResult
    private func writeSubtitles(_ id: Int, track: SubtitleTrack) -> Task<Bool, Never> {
        let previous = subtitleWrites[id], url = cuesURL(id)
        let task = Task.detached(priority: .utility) { () async -> Bool in
            _ = await previous?.value
            // Deleted meanwhile (deleteEpisode cancels the last write of the chain): what a write
            // before this one left after the delete goes too.
            if Task.isCancelled { try? FileManager.default.removeItem(at: url); return false }
            guard let data = try? JSONEncoder().encode(track), !Task.isCancelled else { return false }
            do { try data.write(to: url, options: .atomic) } catch { return false }
            // Deleted while it was written (deleteEpisode cancels): the file goes again.
            if Task.isCancelled { try? FileManager.default.removeItem(at: url); return false }
            return true
        }
        subtitleWrites[id] = task
        Task { [weak self] in
            _ = await task.value
            if self?.subtitleWrites[id] == task { self?.subtitleWrites[id] = nil }
        }
        return task
    }
    /// The same, waited for; throws when the file could not be written.
    private func writeSubtitlesNow(_ id: Int, track: SubtitleTrack) async throws {
        guard await writeSubtitles(id, track: track).value else { throw PudgeError.invalid("Could not save the subtitles") }
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
        // A "watched" mark or a place the Mac has not got yet stays (the record without the video)
        // and is sent when the Mac answers: deleting the download lost it (AniList progress too).
        let unsent = localEpisodes[id].map { $0.unsent && !isOtherMac($0) } ?? false
        subtitleWrites.removeValue(forKey: id)?.cancel()
        let files = unsent ? [videoURL(id), cuesURL(id)] : [videoURL(id), cuesURL(id), metaURL(id)]
        PartialFileShare.shared.forget(url: videoURL(id))
        for url in files { try? FileManager.default.removeItem(at: url) }
        loadLocalEpisodes()
        if unsent {
            AppLog.write("video", "episode \(id) deleted; its mark waits for the Mac")
            Task { await sendVideoPositions() }
        }
    }

    /// The player could not play a download (PlaybackWatchdog): it goes and comes again from the
    /// Mac. Kept when the Mac's list does not have it (nothing to download it from now).
    func redownload(_ id: Int) {
        guard let item = episodes.first(where: { $0.id == id }) else {
            AppLog.write("video", "episode \(id) not downloaded again: not on the Mac's list now")
            app.message = "Connect to the Mac to download this episode again."
            return
        }
        AppLog.write("video", "episode \(id): deleted and downloaded again (it did not play here)")
        deleteEpisode(id)
        Task { await download(item) }
    }

    /// Downloads episodes one after another (the Mac prepares each in turn), one at a time so
    /// the first is never slowed by the others: of one anime the episode nearest to the next to
    /// watch first (EpisodeDownloadOrder), then on from it.
    func downloadAll(_ items: [EpisodeItem]) {
        let pending = EpisodeCopies.unique(items).filter { item in
            localEpisodes[item.id] == nil && videoJobs[item.id] == nil && !videoQueue.contains { $0.id == item.id } && otherCopy(of: item) == nil
        }
        guard !pending.isEmpty else { return }
        let idle = videoQueue.isEmpty
        // The one downloading stays first; the rest in order of distance from the next to watch.
        let running = idle ? [] : [videoQueue[0]]
        let waiting = Array(videoQueue.dropFirst(idle ? 0 : 1)) + pending
        var nextByAnime: [Int: Int] = [:]
        for mediaId in Set(waiting.map(\.mediaId)) { nextByAnime[mediaId] = nextToWatchNumber(mediaId) }
        videoQueue = running + EpisodeDownloadOrder.arrange(waiting, mediaId: { $0.mediaId }, number: { $0.number },
                                                            next: { nextByAnime[$0] })
        guard idle else { return }
        Task {
            while let next = videoQueue.first {
                if localEpisodes[next.id] == nil { await download(next) }
                videoQueue.removeAll { $0.id == next.id }
            }
        }
    }

    /// The next episode to watch of an anime (the first unwatched after the last watched, here
    /// or on the Mac; without a watched one the lowest number), for the download order.
    func nextToWatchNumber(_ mediaId: Int) -> Int? {
        guard mediaId > 0, let group = animeGroups.first(where: { $0.mediaId == mediaId }) else { return nil }
        let watched = group.episodes.map { item -> (number: Int, watched: Bool) in
            (number: item.number, watched: item.watched || finishedHere(item))
        }
        let next = EpisodeDownloadOrder.nextToWatch(watched)
        guard group.progress > 0 else { return next }
        // AniList's progress counts too (episodes watched before they were on this list).
        return max(next ?? 0, group.progress + 1)
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
        markInterrupted(id, false)
        // Cancelled while waiting (cut earlier): its kept bytes go too.
        if videoJobs[id] == nil { Self.removePartial(videoFolder.appendingPathComponent("\(id).part")) }
        videoQueue.removeAll { $0.id == id }
        videoCancelled.insert(id)
        videoTasks[id]?.cancel()
        videoTasks[id] = nil
        videoJobs[id] = nil
    }

    /// The copy a download or a stream asks the Mac for: the one chosen (or Settings'), the
    /// original on a Mac without smaller copies.
    func videoQuality(_ chosen: VideoQuality?, key: String) -> VideoQuality {
        guard app.macSupports("video-quality") else { return .original }
        return chosen ?? VideoQuality.stored(key)
    }

    /// `quality`: the copy to download (nil: Settings → Downloads → Download quality).
    func download(_ episode: EpisodeItem, quality chosen: VideoQuality? = nil) async {
        guard videoJobs[episode.id] == nil else { return }
        let quality = videoQuality(chosen, key: VideoQuality.downloadKey)
        // A second Download of the same episode (another file of it on the Mac) does nothing.
        if let other = otherCopy(of: episode) {
            AppLog.write("video", "episode \(episode.id) not downloaded: episode \(episode.number) is already here or downloading (\(other))")
            markInterrupted(episode.id, false)
            return
        }
        videoCancelled.remove(episode.id)
        videoJobs[episode.id] = VideoJob(phase: "Preparing on the Mac…", fraction: 0)
        videoJobItems[episode.id] = episode
        let background = BackgroundTaskBox()
        let episodeId = episode.id
        background.id = UIApplication.shared.beginBackgroundTask(withName: "Pudge video") {
            // iOS ends the background time: the download pauses with the app and goes on when
            // Pudge is open again (a cut connection resumes where it stopped, TransferRetry).
            // It was cancelled: what it had received was deleted and "Download all" dropped it.
            // Called on the main thread; ended before returning (a task still running when the
            // handler returns gets the app killed).
            MainActor.assumeIsolated {
                AppLog.write("video", "episode \(episodeId): background time ended; the download continues when Pudge is open again")
                if background.id != .invalid { UIApplication.shared.endBackgroundTask(background.id); background.id = .invalid }
            }
        }
        defer {
            videoJobs[episode.id] = nil
            videoJobItems[episode.id] = nil
            downloadingCopies[episode.id] = nil
            if background.id != .invalid { UIApplication.shared.endBackgroundTask(background.id); background.id = .invalid }
        }
        func checkCancelled() throws { if videoCancelled.contains(episode.id) { throw CancellationError() } }
        do {
            let client = try connection()
            // Remembered until it is done: cut off by a Mac that stays away or by Pudge being
            // closed, it goes on by itself when the Mac answers (resumeInterruptedDownloads).
            markInterrupted(episode.id, true, server: client.mac.serverId)
            guard var state = try await preparedVideo(episode, client: client, quality: quality) else { return }
            guard state.state == "ready", var video = state.video else {
                throw PudgeError.invalid(state.message.isEmpty ? "The Mac could not prepare this episode" : state.message)
            }
            downloadingCopies[episode.id] = DownloadingCopy(video: video, quality: quality)
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
                try await fetch("/api/v2/resources/\(video.resourceId)/content?sha256=\(video.sha256)", client: client, to: partial, id: episode.id,
                                sha256: video.sha256, sizeBytes: video.sizeBytes)
            } catch let refused as MacRefusedFile {
                // The Mac's copy went away (removed for space, or the episode changed) after it said
                // "ready": it is prepared again once and downloaded from the new copy.
                AppLog.write("video", "episode \(episode.id): \(refused.localizedDescription); preparing it again")
                try checkCancelled()
                guard let again = try await preparedVideo(episode, client: client, quality: quality) else { return }
                guard again.state == "ready", let fresh = again.video else {
                    throw PudgeError.invalid(again.message.isEmpty ? "The Mac could not prepare this episode" : again.message)
                }
                state = again; video = fresh
                downloadingCopies[episode.id] = DownloadingCopy(video: fresh, quality: quality)
                try await fetch("/api/v2/resources/\(video.resourceId)/content?sha256=\(video.sha256)", client: client, to: partial, id: episode.id,
                                sha256: video.sha256, sizeBytes: video.sizeBytes)
            }
            try checkCancelled()
            videoJobs[episode.id] = VideoJob(phase: "Verifying…", fraction: 1)
            let target = videoURL(episode.id)
            let sha = video.sha256, size = video.sizeBytes
            do {
                try await Task.detached(priority: .utility) {
                    try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size)
                }.value
            } catch let integrity as PudgeError {
                // Other bytes than the Mac's copy (e.g. a resumed transfer stitched wrong): once
                // more from 0 (DownloadCheck), then the error.
                guard DownloadCheck.next(after: .integrity, attempt: 1) == .redownload else { throw integrity }
                AppLog.write("video", "episode \(episode.id) failed its check (\(integrity.localizedDescription)): downloading it again from 0")
                Self.removePartial(partial)
                try checkCancelled()
                videoJobs[episode.id] = VideoJob(phase: "Downloading…", fraction: 0)
                try await fetch("/api/v2/resources/\(video.resourceId)/content?sha256=\(video.sha256)", client: client, to: partial, id: episode.id,
                                sha256: video.sha256, sizeBytes: video.sizeBytes)
                try checkCancelled()
                videoJobs[episode.id] = VideoJob(phase: "Verifying…", fraction: 1)
                try await Task.detached(priority: .utility) {
                    try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size)
                }.value
            }
            try checkCancelled()
            try? FileManager.default.removeItem(at: target)
            try FileManager.default.moveItem(at: partial, to: target)
            // The Mac's bytes, but can AVFoundation play them here? (tracks, decoder, duration).
            // Not listed as downloaded when not: the same bytes would only come again.
            let expectedSeconds = Double(state.durationMs ?? Self.milliseconds(episode.duration)) / 1000
            let (probe, format) = await VideoFileProbe.probe(target)
            if case .bad(let reason) = DownloadCheck.judge(probe, expectedSeconds: expectedSeconds) {
                AppLog.write("video", "episode \(episode.id) downloaded but not playable here: \(reason) (\(format))")
                try? FileManager.default.removeItem(at: target)
                throw PudgeError.invalid("This episode can't be played on this \(PudgeTheme.device): \(reason).")
            }
            AppLog.write("video", "episode \(episode.id) checked: \(VideoFileProbe.summary(probe)) \(format)")
            // A player watching it meanwhile goes on from the finished file.
            PartialFileShare.shared.moved(sha, to: target, sizeBytes: size)
            Self.removePartial(partial)
            try await writeSubtitlesNow(episode.id, track: track)
            var local = LocalEpisode(id: episode.id, title: episode.title, number: episode.number, sha256: video.sha256,
                                     sizeBytes: video.sizeBytes, durationMs: state.durationMs ?? Self.milliseconds(episode.duration),
                                     positionMs: Self.milliseconds(episode.position), positionDirty: false, savedAt: Date().timeIntervalSince1970,
                                     mediaId: episode.mediaId, subtitleRevision: episode.subtitleRevision, serverId: client.mac.serverId)
            // Downloaded again after it was watched (here or on the Mac): never deleted on its own.
            if episode.watched || pendingEpisodes[episode.id]?.watchedSent != nil { local.kept = true }
            local.quality = quality == .original ? nil : quality.rawValue
            local.skipSegments = episode.skipSegments
            // Deleted earlier with a mark the Mac has not got yet: the mark comes along.
            if let left = pendingEpisodes.removeValue(forKey: episode.id) {
                local.watchedSeconds = left.watchedSeconds; local.watchedSent = left.watchedSent; local.watchedAt = left.watchedAt
                if left.positionDirty { local.positionMs = left.positionMs; local.positionDirty = true; local.savedAt = left.savedAt }
            }
            try JSONEncoder().encode(local).write(to: metaURL(episode.id), options: .atomic)
            loadLocalEpisodes()
            markInterrupted(episode.id, false, server: client.mac.serverId)
            AppLog.write("video", "downloaded episode \(episode.id) \(video.sizeBytes) bytes (\(quality.rawValue))")
            let episodeId = episode.id
            Task {
                await saveWithLiveStates(episodeId, track: track)
                if await rememberWords(track.cues.flatMap(\.tokens)) { wordsChecked.insert(episodeId) }
            }
        } catch {
            // Cut by the network or a Mac that stayed away (an update): the bytes received stay
            // and the download goes on from them when the Mac answers. Anything else starts over.
            let resumable = TransferRetry.isTransient(error) && !videoCancelled.contains(episode.id)
            if !resumable {
                Self.removePartial(videoFolder.appendingPathComponent("\(episode.id).part"))
                markInterrupted(episode.id, false)
            }
            // Failed after the video was moved in but before its record was written: a video no
            // list showed (Settings counted it, nothing deleted it).
            if !FileManager.default.fileExists(atPath: metaURL(episode.id).path) {
                try? FileManager.default.removeItem(at: videoURL(episode.id))
                try? FileManager.default.removeItem(at: cuesURL(episode.id))
            }
            if !videoCancelled.contains(episode.id) {
                AppLog.write("video", "episode \(episode.id) not downloaded\(resumable ? " (kept to resume)" : ""): \(error.localizedDescription)")
                app.message = resumable
                    ? "The Mac does not answer. The download goes on where it stopped once the Mac is back."
                    : error.localizedDescription
            }
        }
    }

    /// Asks the Mac to prepare the episode and waits until it is ready (nil: cancelled here).
    /// "idle" means the Mac restarted or removed its copy meanwhile: it is asked again. A Mac
    /// that does not answer for a moment (restarting, Wi-Fi hiccup) is waited for about a minute.
    /// `streaming`: for watching from the Mac (its progress and Cancel are the stream's).
    private func preparedVideo(_ episode: EpisodeItem, client: MacConnection, streaming: Bool = false,
                               quality: VideoQuality = .original) async throws -> EpisodeState? {
        let route = "episodes/\(episode.id)/prepare"
        // The state of this copy (another copy may be ready or preparing meanwhile).
        let poll = "episodes/\(episode.id)" + (quality == .original ? "" : "?quality=\(quality.rawValue)")
        let request = VideoQualityRequest(quality)
        var state = try await client.media(route, post: request, as: EpisodeState.self)
        var unanswered = 0
        while state.state == "preparing" || state.state == "idle" {
            if (streaming ? streamCancelled : videoCancelled).contains(episode.id) { return nil }
            let job = VideoJob(phase: "Preparing on the Mac…", fraction: state.progress)
            if streaming { streamJobs[episode.id] = job } else { videoJobs[episode.id] = job }
            try await Task.sleep(nanoseconds: 2_000_000_000)
            do {
                if state.state == "idle" {
                    state = try await client.media(route, post: request, as: EpisodeState.self)
                } else {
                    state = try await client.media(poll, as: EpisodeState.self)
                }
                unanswered = 0
            } catch {
                unanswered += 1
                guard TransferRetry.isTransient(error), unanswered < 30 else { throw error }
            }
        }
        return state
    }

    private func fetch(_ route: String, client: MacConnection, to target: URL, id: Int, sha256: String, sizeBytes: Int64) async throws {
        defer { videoTasks[id] = nil }
        try await transfer(route, client: client, to: target, sha256: sha256, sizeBytes: sizeBytes,
                           cancelled: { self.videoCancelled.contains(id) },
                           started: { [weak self] task in self?.videoTasks[id] = task }) { [weak self] value in
            if self?.videoJobs[id] != nil { self?.videoJobs[id] = VideoJob(phase: "Downloading…", fraction: value) }
        }
    }

    /// Downloads a file the Mac prepared into `target` (pinned TLS, cellular only when allowed in
    /// Settings). The bytes received stay in `target` when the connection is cut (Wi-Fi drop, the
    /// Mac quitting for an update or restarting): the next attempt asks only for the rest (Range
    /// with If-Range, RangedResume) and attempts go on while the Mac stays away for a while
    /// (TransferRetry.patience). A partial file of the same edition left by an earlier run (Pudge
    /// closed, or the Mac away longer) goes on as well: it started over at 0 % before.
    private func transfer(_ route: String, client: MacConnection, to target: URL, sha256: String, sizeBytes: Int64,
                          cancelled: () -> Bool = { false },
                          started: @escaping (URLSessionTask) -> Void, progress: @escaping @MainActor (Double) -> Void) async throws {
        let expected = PartialDownload(sha256: sha256, sizeBytes: sizeBytes)
        let markerURL = RangedResume.markerURL(for: target)
        let marker = (try? Data(contentsOf: markerURL)).flatMap { try? JSONDecoder().decode(PartialDownload.self, from: $0) }
        var offset = RangedResume.resumeOffset(marker: marker, expected: expected, partialBytes: Self.fileBytes(target))
        if offset == 0 {
            PartialFileShare.shared.forget(url: target)
            try? FileManager.default.removeItem(at: target)
        }
        if marker != expected { try JSONEncoder().encode(expected).write(to: markerURL, options: .atomic) }
        let name = route.split(separator: "?").first.map(String.init) ?? route
        if offset > 0 { AppLog.write("transfer", "\(name) resumes at \(offset) of \(sizeBytes) bytes") }
        progress(RangedResume.fraction(received: offset, total: sizeBytes))
        var attempt = 1
        var lastBytesAt = ProcessInfo.processInfo.systemUptime
        var restarted = false
        while true {
            // Every byte is already here (cut right at the end): the caller verifies it.
            if sizeBytes > 0 && offset == sizeBytes { return }
            // The Mac answers at another address now (it restarted, a new IP): the same file from there.
            let current = app.macConnection.flatMap { $0.mac.serverId == client.mac.serverId ? $0 : nil } ?? client
            do {
                try await rangedAttempt(route, client: current, to: target, offset: offset, total: sizeBytes, sha256: sha256,
                                        started: started, progress: progress)
                if attempt > 1 { AppLog.write("transfer", "\(name) done after \(attempt) attempts") }
                return
            } catch let mismatch as RangeMismatch {
                // Appending those bytes would stitch the file wrong: the partial goes, from 0 once.
                guard RangedResume.startsOver(alreadyRestarted: restarted), !cancelled() else {
                    AppLog.write("transfer", "\(name) refused: \(mismatch.localizedDescription)")
                    throw MacRefusedFile(status: mismatch.status)
                }
                restarted = true
                AppLog.write("transfer", "\(name): \(mismatch.localizedDescription); starting over from 0")
                PartialFileShare.shared.reset(sha256, url: target)
                try? FileManager.default.removeItem(at: target)
                offset = 0
                progress(0)
            } catch {
                let now = ProcessInfo.processInfo.systemUptime
                let have = Self.fileBytes(target) ?? 0
                if have > offset { lastBytesAt = now; attempt = 1 }  // it moved on: the quick attempts start over
                offset = have
                guard TransferRetry.shouldRetry(error, attempt: attempt, stalledFor: now - lastBytesAt), !cancelled() else {
                    AppLog.write("transfer", "\(name) failed (attempt \(attempt), \(offset) of \(sizeBytes) bytes kept): \(error.localizedDescription)")
                    throw error
                }
                AppLog.write("transfer", "\(name) cut (\((error as NSError).code)): attempt \(attempt + 1) resumes at \(offset) of \(sizeBytes) bytes")
                try await Task.sleep(nanoseconds: UInt64(TransferRetry.delay(after: attempt) * 1_000_000_000))
                if cancelled() { throw CancellationError() }
                attempt += 1
            }
        }
    }

    private func rangedAttempt(_ route: String, client: MacConnection, to target: URL, offset: Int64, total: Int64, sha256: String,
                               started: @escaping (URLSessionTask) -> Void, progress: @escaping @MainActor (Double) -> Void) async throws {
        var request = try client.request(route)
        for (field, value) in RangedResume.headers(offset: offset, sha256: sha256) { request.setValue(value, forHTTPHeaderField: field) }
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = 6 * 3600
        config.allowsCellularAccess = UserDefaults.standard.bool(forKey: "pudge.cellularDownloads")
        let throttle = ProgressThrottle()
        throttle.last = RangedResume.fraction(received: offset, total: total)
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let delegate = RangedFileDelegate(pin: client.delegate.pin, origin: client.delegate.origin, target: target,
                                              offset: offset, total: total, key: sha256, report: { bytes in
                let value = RangedResume.fraction(received: bytes, total: total)
                guard abs(value - throttle.last) >= 0.01 || value >= 1 else { return }
                throttle.last = value
                Task { @MainActor in progress(value) }
            }, finish: { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            })
            let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
            let task = session.dataTask(with: request)
            started(task)
            task.resume()
            session.finishTasksAndInvalidate()
        }
    }

    private static func fileBytes(_ url: URL) -> Int64? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path) else { return nil }
        return (attributes[.size] as? NSNumber)?.int64Value
    }

    /// A partial download and its marker (RangedResume) go.
    private static func removePartial(_ url: URL) {
        PartialFileShare.shared.forget(url: url)
        try? FileManager.default.removeItem(at: url)
        try? FileManager.default.removeItem(at: RangedResume.markerURL(for: url))
    }

    // MARK: Episode downloads cut off (resumed when the Mac answers again)

    private static func interruptedKey(_ server: String) -> String { "pudge.interruptedEpisodes." + server }
    private static func interrupted(_ server: String) -> Set<Int> {
        Set((UserDefaults.standard.array(forKey: interruptedKey(server)) as? [Int]) ?? [])
    }
    private static func setInterrupted(_ server: String, _ ids: Set<Int>) {
        if ids.isEmpty { UserDefaults.standard.removeObject(forKey: interruptedKey(server)) }
        else { UserDefaults.standard.set(Array(ids).sorted(), forKey: interruptedKey(server)) }
    }
    private func markInterrupted(_ id: Int, _ value: Bool, server: String? = nil) {
        guard let server = server ?? app.macConnection?.mac.serverId else { return }
        var ids = Self.interrupted(server)
        if value { ids.insert(id) } else { ids.remove(id) }
        Self.setInterrupted(server, ids)
    }

    /// Episode downloads cut by a Mac that stayed away (an update, a restart) or by Pudge being
    /// closed go on from the bytes already here once the Mac answers (at launch, or when it is
    /// back). They had to be started again by hand, from 0 %.
    func resumeInterruptedDownloads() async {
        guard let server = app.macConnection?.mac.serverId else { return }
        let ids = Self.interrupted(server)
        guard !ids.isEmpty else { return }
        if episodes.isEmpty { await refreshEpisodes() }
        guard episodesError == nil, !episodes.isEmpty else { return }
        let items = episodes.filter { ids.contains($0.id) && localEpisodes[$0.id] == nil }
        // Downloaded meanwhile or no longer on the Mac's list: forgotten (launch cleanup takes its bytes later).
        Self.setInterrupted(server, Set(items.map(\.id)))
        let waiting = items.filter { item in videoJobs[item.id] == nil && !videoQueue.contains { $0.id == item.id } }
        guard !waiting.isEmpty else { return }
        AppLog.write("video", "resuming \(waiting.count) interrupted episode download(s)")
        downloadAll(waiting)
    }

    // MARK: Watching from the Mac (nothing kept on this device)

    /// Watch: the Mac prepares the episode (its progress shows like a download's) and keeps its
    /// copy while the player is open; the player streams it. nil: cancelled or failed (said).
    func prepareStream(_ episode: EpisodeItem) async -> StreamPlayback? {
        guard streamJobs[episode.id] == nil else { return nil }
        streamCancelled.remove(episode.id)
        streamJobs[episode.id] = VideoJob(phase: "Preparing on the Mac…", fraction: 0)
        defer { streamJobs[episode.id] = nil }
        do {
            let client = try connection()
            guard let opened = try await openStream(episode, client: client) else { return nil }
            let state = opened.0, stream = opened.1
            if !streamCancelled.contains(episode.id) { streamJobs[episode.id] = VideoJob(phase: "Subtitles…", fraction: 1) }
            let track: SubtitleTrack
            do { track = try await client.media("episodes/\(episode.id)/subtitles", as: SubtitleTrack.self) }
            catch { stream.close(); throw error }
            guard !streamCancelled.contains(episode.id) else { stream.close(); return nil }
            let record = streamRecord(episode, state: state, serverId: client.mac.serverId)
            streamRecords[episode.id] = record
            AppLog.write("video", "episode \(episode.id) watched from the Mac (\(stream.video.sizeBytes) bytes)")
            return StreamPlayback(item: episode, record: record, track: track, stream: stream)
        } catch {
            if !streamCancelled.contains(episode.id) {
                AppLog.write("video", "episode \(episode.id) not streamed: \(error.localizedDescription)")
                // The Mac did not answer (Pudge closed there, asleep, other Wi-Fi): say so, not a URL error.
                app.message = TransferRetry.isTransient(error) || unreachable(error)
                    ? PudgeModel.unavailable("open Pudge on the Mac (same Wi-Fi), then try again")
                    : error.localizedDescription
            }
            return nil
        }
    }

    /// The row stays ("Cancelling…") until the preparation in progress has returned: Watch
    /// tapped again meanwhile would revive it and open a second stream.
    func cancelStream(_ id: Int) {
        guard streamJobs[id] != nil else { return }
        streamCancelled.insert(id)
        streamJobs[id] = VideoJob(phase: "Cancelling…", fraction: 0)
    }

    /// Try again after the Mac stopped answering: prepared again if its copy went, with a new
    /// stream (the player goes on where it stopped). nil: cancelled.
    func reopenStream(_ episode: EpisodeItem) async throws -> MacVideoStream? {
        let client = try connection()
        guard await app.reachMac() else { throw PudgeError.invalid("The Mac still does not answer.") }
        streamCancelled.remove(episode.id)
        defer { streamJobs[episode.id] = nil }
        return try await openStream(episode, client: client)?.1
    }

    /// Where watching from the Mac starts (ms): a place here the Mac has not got yet, else the Mac's.
    func streamPositionMs(_ item: EpisodeItem) -> Int64 {
        if let left = pendingEpisodes[item.id], left.positionDirty { return left.positionMs }
        return Self.milliseconds(item.position)
    }

    /// The player of an episode watched from the Mac closed: what was watched goes to the Mac
    /// (kept on this device until it has), and the list shows the new place.
    func endStream(_ id: Int) {
        streamRecords[id] = nil
        Task { await refreshEpisodes() }
    }

    /// Ready on the Mac with its copy held for this player. A Mac without streams (older Pudge)
    /// keeps a copy a phone asked for two hours anyway. nil: cancelled here.
    private func openStream(_ episode: EpisodeItem, client: MacConnection) async throws -> (EpisodeState, MacVideoStream)? {
        // Settings → Downloads → Stream quality (the original on a Mac without smaller copies).
        // Downloading meanwhile: the copy downloaded, so its part already here plays from the device.
        let quality = downloadingCopies[episode.id]?.quality ?? videoQuality(nil, key: VideoQuality.streamKey)
        // A copy removed between "ready" and opening the stream is prepared again.
        for _ in 0..<3 {
            guard let state = try await preparedVideo(episode, client: client, streaming: true, quality: quality) else { return nil }
            guard state.state == "ready", let video = state.video else {
                throw PudgeError.invalid(state.message.isEmpty ? "The Mac could not prepare this episode" : state.message)
            }
            guard app.macSupports("video-stream") else {
                return (state, MacVideoStream(episodeId: episode.id, video: video, lease: nil, connection: client, durationMs: state.durationMs))
            }
            let opened = try await client.media("episodes/\(episode.id)/stream", post: VideoQualityRequest(quality), as: VideoStreamOpened.self)
            if streamCancelled.contains(episode.id) {
                if let lease = opened.stream {
                    _ = try? await client.media("streams/\(lease.id)/close", post: EmptyRequest(), as: OkResponse.self)
                }
                return nil
            }
            if let lease = opened.stream, let ready = opened.video {
                return (state, MacVideoStream(episodeId: episode.id, video: ready, lease: lease, connection: client, durationMs: opened.durationMs ?? state.durationMs, quality: quality))
            }
        }
        throw PudgeError.invalid("The Mac could not keep this episode ready. Try again.")
    }

    /// The record of an episode watched from the Mac: what this device has not sent yet for it
    /// (an earlier stream, a deleted download), else the Mac's place.
    private func streamRecord(_ episode: EpisodeItem, state: EpisodeState, serverId: String) -> LocalEpisode {
        if var left = pendingEpisodes[episode.id] {
            if !left.positionDirty { left.positionMs = Self.milliseconds(episode.position) }
            return left
        }
        return LocalEpisode(id: episode.id, title: episode.title, number: episode.number, sha256: state.video?.sha256 ?? "",
                            sizeBytes: state.video?.sizeBytes ?? 0,
                            durationMs: state.durationMs ?? Self.milliseconds(episode.duration),
                            positionMs: Self.milliseconds(episode.position), positionDirty: false, savedAt: Date().timeIntervalSince1970,
                            mediaId: episode.mediaId, subtitleRevision: episode.subtitleRevision, serverId: serverId)
    }

    private static func milliseconds(_ seconds: Double) -> Int64 { seconds.isFinite ? Int64(max(0, seconds) * 1000) : 0 }

    /// Saves an episode's record: a download's with its video; one watched from the Mac stays on
    /// this device only while something waits for the Mac (sent like a deleted download's).
    private func keepRecord(_ item: LocalEpisode) {
        if localEpisodes[item.id] != nil {
            localEpisodes[item.id] = item
            try? JSONEncoder().encode(item).write(to: metaURL(item.id), options: .atomic)
            return
        }
        streamRecords[item.id] = item
        if item.unsent && !isOtherMac(item) {
            pendingEpisodes[item.id] = item
            try? JSONEncoder().encode(item).write(to: metaURL(item.id), options: .atomic)
        } else if pendingEpisodes.removeValue(forKey: item.id) != nil {
            try? FileManager.default.removeItem(at: metaURL(item.id))
        }
    }

    /// The place of the episode playing now goes to the Mac (every 15 s while it plays, on pause,
    /// after a seek, when the app goes to the background; PositionSyncSchedule). Kept on this device
    /// first: a Mac away gets it once it answers (the newest place wins by its time there).
    func syncPlayingPosition(_ id: Int, positionMs: Int64, seconds: IndexSet, background: Bool = false) {
        recordPlayed(id, seconds: seconds, positionMs: positionMs)
        guard background else { Task { await sendVideoPositions() }; return }
        // Going to the background: a little time to reach the Mac before iOS suspends the app.
        // Ended when iOS ends the background time too (a task left running gets the app killed).
        let task = BackgroundTaskBox()
        task.id = UIApplication.shared.beginBackgroundTask(withName: "Pudge episode place") {
            // Called on the main thread.
            MainActor.assumeIsolated {
                if task.id != .invalid { UIApplication.shared.endBackgroundTask(task.id); task.id = .invalid }
            }
        }
        Task {
            await sendVideoPositions()
            if task.id != .invalid { UIApplication.shared.endBackgroundTask(task.id); task.id = .invalid }
        }
    }

    func saveVideoPosition(_ id: Int, positionMs: Int64) {
        guard var item = localEpisodes[id] ?? streamRecords[id] else { return }
        item.positionMs = max(0, positionMs); item.positionDirty = true; item.savedAt = Date().timeIntervalSince1970
        keepRecord(item)
    }

    /// Merges played seconds; once 65% of the episode (at least 3 min) was really played and the
    /// end is reached, the episode counts like a full watch in mpv (AniList, schedule, cleanup).
    func recordPlayed(_ id: Int, seconds: IndexSet, positionMs: Int64) {
        guard var item = localEpisodes[id] ?? streamRecords[id] else { return }
        var played = item.watchedSeconds ?? IndexSet()
        played.formUnion(seconds)
        item.watchedSeconds = played
        let duration = Double(item.durationMs) / 1000
        let required = duration > 0 ? max(180, duration * 0.65) : 300
        var counted = false
        if item.watchedSent == nil, duration > 0, Double(played.count) >= required, Double(positionMs) / 1000 >= duration * 0.85 {
            counted = true
            item.watchedSent = false
            item.watchedAt = Date().timeIntervalSince1970
            AppLog.write("video", "episode \(id) watched on this device (\(played.count) s played)")
        }
        keepRecord(item)
        if counted { noteWatched(item) }
        if item.watchedSent == false { Task { await sendVideoPositions() } }
        // Counted: a delay of "immediately" deletes it once the player closes (playerClosed).
        if counted { deleteWatchedEpisodes() }
    }

    private var sendingPositions = false
    private var sendAgain = false

    func sendVideoPositions() async {
        guard let client = app.macConnection, !app.macOffline else { return }
        guard !sendingPositions else { sendAgain = true; return }
        sendingPositions = true
        var counted = false
        defer {
            sendingPositions = false
            if sendAgain { sendAgain = false; Task { await sendVideoPositions() } }
            // The Mac counted a watch: its list (progress, the next episode) is asked for again.
            if counted { Task { await refreshEpisodes() } }
        }
        // Episodes of a previous Mac are never sent (this Mac's episode of that number is another
        // one); those deleted with something unsent come after the downloaded ones.
        for item in Array(localEpisodes.values) + Array(pendingEpisodes.values) where item.watchedSent == false && !isOtherMac(item) {
            do {
                let result = try await client.media("episodes/\(item.id)/watched",
                    post: EpisodeWatchedRequest(duration: Double(item.durationMs) / 1000, activeSeconds: Double(item.watchedSeconds?.count ?? 0)),
                    as: EpisodeWatchedAnswer.self)
                updateEpisode(item.id) { $0.watchedSent = true }
                // Not counted there (too little watched, AniList refused): the Mac's count stands.
                if result.ok { watchedHere[item.id]?.deliveredAt = Date().timeIntervalSince1970; counted = true; finales.mac(result.finale, episode: item.id) }
                else { watchedHere[item.id] = nil }
                if !result.ok { app.message = result.message }
                AppLog.write("video", "episode \(item.id) watched → Mac: \(result.outcome)")
            } catch let refusal as MacRefusal where refusal.status == 422 {
                // A final answer (the episode is gone from the Mac's library): not sent again.
                updateEpisode(item.id) { $0.watchedSent = true }
                watchedHere[item.id] = nil
                AppLog.write("video", "episode \(item.id) watched → Mac refused: \(refusal.localizedDescription)")
            } catch {
                dropStale(item, after: error)
                // The Mac is away: the others wait too. Any other failure keeps only this one back.
                if unreachable(error) { break }
            }
        }
        await sendSubtitleDelays(client)
        await finales.sendAnswers()
        for item in Array(localEpisodes.values) + Array(pendingEpisodes.values) where item.positionDirty && !isOtherMac(item) {
            do {
                let ranges = item.watchedSent == nil ? item.watchedSeconds.map { WatchedRanges.ranges($0) } : nil
                let receipt = try await client.media("episodes/\(item.id)/position",
                    post: EpisodePositionRequest(seconds: Double(item.positionMs) / 1000, duration: Double(item.durationMs) / 1000,
                                                 observedAt: item.savedAt, ranges: ranges), as: EpisodePositionReceipt.self)
                updateEpisode(item.id) { sent in if sent.positionMs == item.positionMs { sent.positionDirty = false } }
                if receipt.counted == true {
                    // Part watched on the Mac, the rest here: counted there like a full watch.
                    updateEpisode(item.id) { if $0.watchedSent == nil { $0.watchedSent = true; $0.watchedAt = Date().timeIntervalSince1970 } }
                    noteWatched(item); watchedHere[item.id]?.deliveredAt = Date().timeIntervalSince1970
                    finales.mac(receipt.finale, episode: item.id)
                    counted = true
                    AppLog.write("video", "episode \(item.id) watched on the Mac and here together → counted")
                }
            } catch {
                dropStale(item, after: error)
                // As the watched loop: the Mac away stops the others; a place it refuses (the
                // episode gone there) no longer keeps every later one from being sent.
                if unreachable(error) { break }
            }
        }
        if counted { deleteWatchedEpisodes() }
    }

    /// Subtitle timings changed here go to the Mac (its mpv delay for the same subtitle). A Mac
    /// without the route (older) or without the episode refuses: the timing stays on this device.
    private func sendSubtitleDelays(_ client: MacConnection) async {
        for (id, ms) in subtitleDelays.delays.unsent {
            if let local = localEpisodes[id] ?? pendingEpisodes[id], isOtherMac(local) { subtitleDelays.sent(ms, for: id); continue }
            do {
                _ = try await client.media("episodes/\(id)/subtitle-delay", post: SubtitleDelayRequest(ms: ms), as: OkResponse.self)
                subtitleDelays.sent(ms, for: id)
            } catch let refusal as MacRefusal where refusal.status == 404 || refusal.status == 422 {
                subtitleDelays.sent(ms, for: id)
                AppLog.write("video", "episode \(id) subtitle timing kept here: \(refusal.localizedDescription)")
            } catch {
                if unreachable(error) { return }
            }
        }
    }

    /// Changes the record of a downloaded episode, or of one deleted while something waited for
    /// the Mac (that record goes once nothing waits).
    private func updateEpisode(_ id: Int, _ change: (inout LocalEpisode) -> Void) {
        // Playing from the Mac now: the player's record learns it too (a mark is sent once).
        if localEpisodes[id] == nil, var record = streamRecords[id] {
            change(&record)
            streamRecords[id] = record
        }
        if var item = localEpisodes[id] {
            change(&item)
            localEpisodes[id] = item
            try? JSONEncoder().encode(item).write(to: metaURL(id), options: .atomic)
        } else if var item = pendingEpisodes[id] {
            change(&item)
            if item.unsent {
                pendingEpisodes[id] = item
                try? JSONEncoder().encode(item).write(to: metaURL(id), options: .atomic)
            } else {
                pendingEpisodes[id] = nil
                try? FileManager.default.removeItem(at: metaURL(id))
            }
        }
    }

    // MARK: Watched downloads deleted on their own (as on the Mac)

    static let macDeleteWatchedKey = "pudge.macDeleteWatchedHours"
    /// The Mac's agent.delete_after_watched_hours, from its last episode list (nil: not sent).
    var macDeleteWatchedHours: Double? { UserDefaults.standard.object(forKey: Self.macDeleteWatchedKey) as? Double }
    var watchedCleanupChoice: WatchedCleanupChoice {
        WatchedCleanupChoice(stored: UserDefaults.standard.string(forKey: WatchedCleanupChoice.defaultsKey))
    }
    var watchedCleanupDelay: TimeInterval? { watchedCleanupChoice.delay(macHours: macDeleteWatchedHours) }

    /// The player shows it now: not deleted until it closes.
    func playerOpened(_ id: Int) { playingEpisodes.insert(id); app.videoOnScreen = true; finales.playerOpened(id) }
    func playerClosed(_ id: Int) {
        playingEpisodes.remove(id)
        deleteWatchedEpisodes()
        finales.playerClosed()
        // An alert that waited shows once the player's dismissal has finished.
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard let self else { return }
            self.app.videoOnScreen = !self.playingEpisodes.isEmpty
        }
    }

    private func cleanupState(_ item: LocalEpisode) -> WatchedCleanupState {
        WatchedCleanupState(watchedAt: item.watchedAt, kept: item.kept == true, positionMs: item.positionMs, durationMs: item.durationMs,
                            busy: playingEpisodes.contains(item.id) || videoJobs[item.id] != nil,
                            watchedElsewhere: Self.watchedElsewhere(item))
    }

    /// Watched on another device: marked so since this was kept, or (copies adopted earlier) counted
    /// on the Mac with nothing played here and the place at the start.
    static func watchedElsewhere(_ item: LocalEpisode) -> Bool {
        if let marked = item.watchedElsewhere { return marked }
        return item.watchedSent == true && item.positionMs == 0 && (item.watchedSeconds?.isEmpty ?? true)
    }

    /// When the download of a watched episode goes (seconds since 1970); nil: it stays.
    func watchedDeletion(_ id: Int) -> Double? {
        guard let item = localEpisodes[id] else { return nil }
        return WatchedCleanup.deleteAt(cleanupState(item), delay: watchedCleanupDelay)
    }

    /// "Keep": this download is not deleted on its own any more.
    func keepEpisode(_ id: Int) {
        updateEpisode(id) { $0.kept = true }
        AppLog.write("video", "episode \(id) kept (not deleted automatically)")
    }

    /// Deletes the watched downloads whose delay has passed (launch, foreground, a counted watch,
    /// the player closed, the Mac's setting or this device's choice changed).
    func deleteWatchedEpisodes(now: Double = Date().timeIntervalSince1970) {
        let delay = watchedCleanupDelay
        let due = localEpisodes.values.filter { WatchedCleanup.isDue(cleanupState($0), delay: delay, now: now) }
        for item in due {
            AppLog.write("video", "episode \(item.id) watched: deleted from this device after the delay (\(PudgeTheme.size(item.sizeBytes)))")
            deleteEpisode(item.id)
        }
    }

    /// Kept episode records without a video: a deleted download's place or mark waiting for the Mac.
    var episodeMarkIds: Set<Int> { Set(pendingEpisodes.keys) }
    /// Something downloads now (its partial files are not leftovers).
    var downloadsInProgress: Bool {
        !videoJobs.isEmpty || !videoQueue.isEmpty || !readingJobs.isEmpty || !ocrJobs.isEmpty || !streamJobs.isEmpty
    }

    /// A deleted episode's mark the Mac refuses (e.g. the episode is gone there) waits a month at most.
    private func dropStale(_ item: LocalEpisode, after error: Error) {
        guard !unreachable(error), pendingEpisodes[item.id] != nil, Date().timeIntervalSince1970 - item.savedAt > 30 * 86_400 else { return }
        pendingEpisodes[item.id] = nil
        try? FileManager.default.removeItem(at: metaURL(item.id))
        AppLog.write("video", "episode \(item.id): mark of the deleted download dropped (\(error.localizedDescription))")
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

    /// Readers (light novel, manga) open now: observed every 5 s until their screen closes.
    private var openReaders = Set<String>()
    /// A light novel or manga is open: the anime list is not refreshed meanwhile (AnimeLiveRefresh).
    var readerOnScreen: Bool { !openReaders.isEmpty }
    /// Seconds since the last tap, scroll or page turn in a reader.
    var secondsSinceTouch: Double { ProcessInfo.processInfo.systemUptime - lastTouch }

    /// A tap, scroll or page turn in the reader of this item.
    func touched(kind: String, item: Int) { lastTouch = ProcessInfo.processInfo.systemUptime; touchedKey = "\(kind):\(item)" }
    /// This item was the last one touched, less than 5 minutes ago (a reader left open under another one stops counting).
    func isReading(kind: String, item: Int) -> Bool {
        ProcessInfo.processInfo.systemUptime - lastTouch <= 300 && touchedKey == "\(kind):\(item)"
    }

    /// What is on screen now; `active` false ends the current run (idle, paused, in background).
    func observe(kind: String, item: Int, active: Bool, at position: [String: Double]) {
        let key = "\(kind):\(item)"
        if kind != "anime" { openReaders.insert(key) }
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
    func stop(kind: String, item: Int) { samples["\(kind):\(item)"] = nil; openReaders.remove("\(kind):\(item)") }
}

/// Wide pages and the usual page shape of downloaded page files, from their image headers (no
/// page is decoded). A file that cannot be read is left out.
func mangaPageShapes(_ files: [URL?]) -> MangaSpreadPages {
    var sizes: [Int: CGSize] = [:]
    for (index, file) in files.enumerated() {
        guard let file, let source = CGImageSourceCreateWithURL(file as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any],
              let width = (properties[kCGImagePropertyPixelWidth as String] as? NSNumber)?.doubleValue,
              let height = (properties[kCGImagePropertyPixelHeight as String] as? NSNumber)?.doubleValue else { continue }
        // EXIF orientations 5 to 8 turn the picture a quarter: UIImage shows it turned.
        let turned = ((properties[kCGImagePropertyOrientation as String] as? NSNumber)?.intValue ?? 1) >= 5
        sizes[index] = turned ? CGSize(width: height, height: width) : CGSize(width: width, height: height)
    }
    return MangaSpreads.pages(sizes: sizes)
}
