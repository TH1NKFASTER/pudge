import Foundation

/// One file Pudge keeps on this iPhone/iPad. `path` is relative to its root: `store/…` (the
/// app's data folder, Application Support/PudgeAudio), `tmp/…` (the temporary folder, where
/// URLSession leaves the file of a download cut off when the app ends) or `sessions/…`
/// (Library/Caches/com.apple.nsurlsessiond: the background audio downloads' files).
public struct StoredFile: Equatable, Sendable {
    public let path: String
    public let bytes: Int64
    /// Last change (seconds since 1970).
    public let modified: Double
    public init(path: String, bytes: Int64, modified: Double) { self.path = path; self.bytes = bytes; self.modified = modified }
}

public enum StorageKind: String, CaseIterable, Sendable {
    case episodes, reading, audio, covers, words, texts, database, logs
    /// Not reachable from any list: "Other files — Clear".
    case other
}

/// What the files are checked against: every byte counted belongs to something listed, or it is
/// an "other" file (a record removed while its file stayed, an interrupted download, a
/// temporary file of URLSession).
public struct StorageReferences: Equatable, Sendable {
    /// Downloaded episodes with their video (listed in Settings → Storage → Episodes).
    public var episodes: Set<Int>
    /// Episode records without a video: a deleted download's place or "watched" mark waiting for the Mac.
    public var episodeMarks: Set<Int>
    /// Folders of the listed light-novel and manga volumes ("ln-3", "manga-12").
    public var readingFolders: Set<String>
    /// Audiobook files of the store's resource records, relative to the store ("resources/x.m4a").
    public var audioFiles: Set<String>
    /// Something downloads now (episode, volume, OCR or audiobook): its partial files are not other files.
    public var downloading: Bool
    /// An audiobook transfer can resume (queued, running or paused with resume data): the
    /// background session's files belong to it.
    public var audioResumable: Bool
    public init(episodes: Set<Int> = [], episodeMarks: Set<Int> = [], readingFolders: Set<String> = [], audioFiles: Set<String> = [],
                downloading: Bool = false, audioResumable: Bool = false) {
        self.episodes = episodes; self.episodeMarks = episodeMarks; self.readingFolders = readingFolders
        self.audioFiles = audioFiles; self.downloading = downloading; self.audioResumable = audioResumable
    }
}

public struct StorageVerdict: Equatable, Sendable {
    public let kind: StorageKind
    /// "Clear" may remove it now (other files that no transfer in progress needs).
    public let clearable: Bool
    /// Removed on its own at launch: nothing references it, it is not new, and no record that
    /// could not be read stands next to it (that waits for "Clear").
    public let onLaunch: Bool
    static func kept(_ kind: StorageKind) -> StorageVerdict { StorageVerdict(kind: kind, clearable: false, onLaunch: false) }
}

public struct StorageSummary: Equatable, Sendable {
    public var bytes: [StorageKind: Int64] = [:]
    public var other: [StoredFile] = []
    public var clearable: [StoredFile] = []
    public init() {}
    public subscript(kind: StorageKind) -> Int64 { bytes[kind] ?? 0 }
    public var total: Int64 { bytes.values.reduce(0, +) }
    public var otherBytes: Int64 { self[.other] }
    public var clearableBytes: Int64 { clearable.reduce(0) { $0 + $1.bytes } }
}

public enum StorageScan {
    /// Leftovers younger than this are not removed at launch (a file being written right now).
    public static let launchMinimumAge: TimeInterval = 3600
    /// A cut episode download is kept this long for resuming before launch removes it.
    public static let resumableAge: TimeInterval = 3 * 86400
    public static let offlineWordsFile = "offline-words.json"

    public static func verdict(_ file: StoredFile, _ refs: StorageReferences, all names: Set<String> = [],
                               now: Double = Date().timeIntervalSince1970) -> StorageVerdict {
        let old = now - file.modified >= launchMinimumAge
        func leftover(blocked: Bool, ambiguous: Bool = false) -> StorageVerdict {
            StorageVerdict(kind: .other, clearable: !blocked, onLaunch: !blocked && !ambiguous && old)
        }
        let parts = file.path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        guard let root = parts.first, parts.count >= 2 else { return leftover(blocked: false, ambiguous: true) }
        let rest = Array(parts.dropFirst())
        switch root {
        case "tmp":
            return leftover(blocked: refs.downloading)
        case "sessions":
            return leftover(blocked: refs.downloading || refs.audioResumable)
        case "store":
            break
        default:
            return leftover(blocked: false, ambiguous: true)
        }
        let name = rest.last ?? ""
        switch rest[0] {
        case "video":
            guard rest.count == 2 else { return leftover(blocked: refs.downloading) }
            if name.hasSuffix(".part") || name.hasSuffix(".part.json") {
                // A cut episode download (and its marker) kept so that it resumes where it stopped:
                // removed at launch only once it is days old (an hour-old one started over).
                let stale = now - file.modified >= resumableAge
                return StorageVerdict(kind: .other, clearable: !refs.downloading, onLaunch: !refs.downloading && stale)
            }
            guard let id = episodeId(name) else { return leftover(blocked: refs.downloading, ambiguous: true) }
            let isRecord = name == "\(id).json"
            if refs.episodes.contains(id) { return .kept(.episodes) }
            if isRecord && refs.episodeMarks.contains(id) { return .kept(.database) }
            // A record that could not be read keeps its video until "Clear" (never on its own).
            let unreadable = !isRecord && names.contains("store/video/\(id).json")
            return leftover(blocked: refs.downloading, ambiguous: isRecord || unreadable)
        case "reading":
            guard rest.count >= 2 else { return leftover(blocked: refs.downloading) }
            let top = rest[1]
            if rest.count == 2 && top.hasSuffix(".pending.json") { return .kept(.database) }
            if refs.readingFolders.contains(top) { return .kept(.reading) }
            if top.hasSuffix(".part") || top.hasSuffix(".unpacking") || top.hasSuffix(".ocr") {
                return leftover(blocked: refs.downloading)
            }
            let unreadable = names.contains("store/reading/\(top)/download.json")
            return leftover(blocked: refs.downloading, ambiguous: unreadable)
        case "resources":
            let relative = rest.joined(separator: "/")
            if refs.audioFiles.contains(relative) {
                // Audiobook resources: covers and read-along texts come with the books.
                let ext = (name as NSString).pathExtension.lowercased()
                if ["jpg", "jpeg", "png", "webp", "heic"].contains(ext) { return .kept(.covers) }
                if ["json", "txt"].contains(ext) { return .kept(.texts) }
                return .kept(.audio)
            }
            return leftover(blocked: refs.downloading || refs.audioResumable)
        case "image-cache":
            return .kept(.covers)
        case "logs":
            return .kept(.logs)
        default:
            break
        }
        guard rest.count == 1 else { return leftover(blocked: false, ambiguous: true) }
        if name == offlineWordsFile { return .kept(.words) }
        if name.hasPrefix("state.sqlite") { return .kept(.database) }
        let ext = (name as NSString).pathExtension.lowercased()
        if ext == "json" { return .kept(.texts) }
        if ["jpg", "jpeg", "png"].contains(ext) { return .kept(.covers) }
        if ext == "partial" || ext == "tmp" { return leftover(blocked: refs.downloading || refs.audioResumable) }
        return leftover(blocked: false, ambiguous: true)
    }

    /// "12.mp4", "12.cues.json", "12.json", "-3.mp4" → the episode's id.
    static func episodeId(_ name: String) -> Int? {
        guard let head = name.split(separator: ".", maxSplits: 1).first else { return nil }
        return Int(head)
    }

    public static func summarize(_ files: [StoredFile], _ refs: StorageReferences, now: Double = Date().timeIntervalSince1970) -> StorageSummary {
        let names = Set(files.map(\.path))
        var summary = StorageSummary()
        for file in files {
            let verdict = verdict(file, refs, all: names, now: now)
            summary.bytes[verdict.kind, default: 0] += max(0, file.bytes)
            if verdict.kind == .other {
                summary.other.append(file)
                if verdict.clearable { summary.clearable.append(file) }
            }
        }
        return summary
    }

    /// What goes on its own at launch (orphans that nothing in progress needs).
    public static func launchCleanup(_ files: [StoredFile], _ refs: StorageReferences, now: Double = Date().timeIntervalSince1970) -> [StoredFile] {
        let names = Set(files.map(\.path))
        return files.filter { verdict($0, refs, all: names, now: now).onLaunch }
    }
}

/// Settings → Storage: an empty category shows "—", not "Zero KB" / "0 bytes".
public enum StorageSizeText {
    public static let empty = "—"

    /// `formatted(bytes)` for something stored, "—" for nothing (0 or less).
    public static func text(_ bytes: Int64, formatted: (Int64) -> String) -> String {
        bytes > 0 ? formatted(bytes) : empty
    }
}
