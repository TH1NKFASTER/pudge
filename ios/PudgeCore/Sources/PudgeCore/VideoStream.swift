import Foundation

/// Watching an episode straight from the Mac (Watch, nothing kept on the device).
///
/// AVPlayer cannot open the Mac's address itself (self-signed pinned certificate, the device's
/// token on every request), so it is given a `pudge-stream://` address and a resource loader
/// asks the Mac for the bytes it wants: byte ranges of the prepared MP4 (moov at the start),
/// in chunks, a little ahead of what AVPlayer has taken.
public enum VideoStreamAddress {
    public static let scheme = "pudge-stream"

    /// The address AVPlayer is given for a prepared episode (a scheme it cannot load, so it asks
    /// the resource loader for every byte).
    public static func assetURL(resourceId: String) -> URL {
        var parts = URLComponents()
        parts.scheme = scheme
        parts.host = "mac"
        parts.path = "/" + resourceId.filter { $0.isLetter || $0.isNumber || $0 == "-" } + ".mp4"
        return parts.url ?? URL(string: "pudge-stream://mac/episode.mp4")!
    }

    /// The Mac's route for the bytes. `stream` names the Mac's stream (it keeps its copy while
    /// the player is open, and the last byte is no download); "play" on a Mac without streams.
    public static func route(resourceId: String, sha256: String, stream: String?) -> String {
        "/api/v2/resources/\(resourceId)/content?sha256=\(sha256)&stream=\(stream ?? "play")"
    }
}

/// Byte ranges of the episode file (both ends included, as in HTTP).
public enum VideoStreamRanges {
    /// The first chunk of a request is small, so playback (and playback after a seek) starts at once.
    public static let firstChunk: Int64 = 256 * 1024
    /// Later chunks: a few seconds of video each over the local network.
    public static let chunk: Int64 = 2 * 1024 * 1024
    /// Chunks asked for at the same time (the next one is on its way while one is handed over).
    public static let readAhead = 2
    /// Chunks one loading request is served with at most (about 30 MB): AVPlayer takes whatever
    /// is handed over, so a request to the end of the file would pull the whole episode; served
    /// in part, it asks again for the rest when it needs it.
    public static let chunksPerRequest = 16
    /// Blocks (StreamBlocks.size) one loading request is served with at most (32 MB), as above.
    public static let blocksPerRequest = 32

    /// What a loading request of AVPlayer asks for, within a file of `size` bytes (nil: nothing
    /// to send). `length` may be anything up to Int.max when `toEnd` is set.
    public static func requested(offset: Int64, length: Int, toEnd: Bool, size: Int64) -> ClosedRange<Int64>? {
        guard size > 0, offset >= 0, offset < size else { return nil }
        let available = size - offset
        let count = toEnd ? available : min(available, Int64(max(0, length)))
        guard count > 0 else { return nil }
        return offset...(offset + count - 1)
    }

    /// The chunks to ask the Mac for, in order: a small first one, then `chunk` bytes each
    /// (`limit` chunks at most, from the start of the range).
    public static func plan(_ range: ClosedRange<Int64>, first: Int64 = firstChunk, chunk: Int64 = chunk,
                            limit: Int = .max) -> [ClosedRange<Int64>] {
        let step = max(1, chunk)
        var length = max(1, min(first, step))
        var result: [ClosedRange<Int64>] = []
        var start = range.lowerBound
        while result.count < max(1, limit) {
            let end = range.upperBound - start < length ? range.upperBound : start + length - 1
            result.append(start...end)
            if end >= range.upperBound { break }
            start = end + 1
            length = step
        }
        return result
    }

    /// What one loading request gets: its first `chunksPerRequest` chunks.
    public static func served(_ range: ClosedRange<Int64>) -> [ClosedRange<Int64>] {
        plan(range, limit: chunksPerRequest)
    }

    /// The Range header of a chunk.
    public static func header(_ range: ClosedRange<Int64>) -> String {
        "bytes=\(range.lowerBound)-\(range.upperBound)"
    }
}

/// A Content-Range header ("bytes 0-1/1234"; the total may be "*").
public struct ContentRange: Equatable, Sendable {
    public let start: Int64
    public let end: Int64
    public let total: Int64?

    public init(start: Int64, end: Int64, total: Int64?) {
        self.start = start; self.end = end; self.total = total
    }

    public static func parse(_ value: String?) -> ContentRange? {
        guard let value else { return nil }
        let text = value.trimmingCharacters(in: .whitespaces)
        guard text.lowercased().hasPrefix("bytes ") else { return nil }
        let parts = text.dropFirst(6).trimmingCharacters(in: .whitespaces).split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2 else { return nil }
        var total: Int64?
        if parts[1] != "*" {
            guard let parsed = number(parts[1]), parsed > 0 else { return nil }
            total = parsed
        }
        let bounds = parts[0].split(separator: "-", maxSplits: 1, omittingEmptySubsequences: false)
        guard bounds.count == 2, let start = number(bounds[0]), let end = number(bounds[1]), end >= start else { return nil }
        if let total, end >= total { return nil }
        return ContentRange(start: start, end: end, total: total)
    }

    /// Digits only (Int64 alone also takes "+5").
    private static func number<S: StringProtocol>(_ text: S) -> Int64? {
        guard !text.isEmpty, text.allSatisfy({ "0123456789".contains($0) }) else { return nil }
        return Int64(text)
    }
}

/// Why the Mac's bytes did not come.
public enum VideoStreamFailure: Error, Equatable, Sendable {
    /// No answer: Wi-Fi gone, the Mac asleep, Pudge closed on the Mac.
    case unreachable
    /// The answer stopped short or the Mac is busy for a moment (asked again).
    case interrupted
    /// The Mac does not accept this device any more.
    case revoked
    /// The Mac's copy changed or went away (prepared again when watching is tried again).
    case changed
    /// Any other answer of the Mac (HTTP status).
    case refused(Int)

    /// Worth asking again at once (a short wait first).
    public var retryable: Bool {
        switch self {
        case .unreachable, .interrupted: return true
        case .revoked, .changed, .refused: return false
        }
    }

    public static func from(status: Int) -> VideoStreamFailure {
        switch status {
        case 401, 403: return .revoked
        // A resource of another revision (409), unknown (404/410) or a range past its end (416).
        case 404, 409, 410, 416: return .changed
        case 500...599: return .interrupted
        default: return .refused(status)
        }
    }

    /// A failed request: nil only when its task was cancelled (`cancelled`: a seek or the player
    /// closed). A request cancelled otherwise was refused by the pinned certificate check (it
    /// cancels the connection): the Mac at that address is not the paired one.
    public static func from(error: Error, cancelled: Bool) -> VideoStreamFailure? {
        if cancelled { return nil }
        if let failure = error as? VideoStreamFailure { return failure }
        if error is CancellationError { return .unreachable }
        let value = error as NSError
        if value.domain == NSURLErrorDomain && value.code == NSURLErrorCancelled { return .revoked }
        if TransferRetry.isTransient(error) { return .unreachable }
        return value.domain == NSURLErrorDomain ? .unreachable : .interrupted
    }
}

/// Checks the Mac's answer to one chunk.
public enum VideoStreamResponse {
    /// The bytes asked for, of the file of `size` bytes (a whole-file answer only for the whole file).
    public static func check(status: Int, contentRange: String?, received: Int64,
                             requested: ClosedRange<Int64>, size: Int64) throws {
        let whole = requested.lowerBound == 0 && requested.upperBound == size - 1
        switch status {
        case 206:
            guard let range = ContentRange.parse(contentRange), range.start == requested.lowerBound,
                  range.end == requested.upperBound, range.total == nil || range.total == size else {
                throw VideoStreamFailure.changed
            }
        case 200 where whole:
            break
        case 200:
            // The Range header was ignored: the bytes would be the wrong ones.
            throw VideoStreamFailure.refused(200)
        default:
            throw VideoStreamFailure.from(status: status)
        }
        guard received == requested.upperBound - requested.lowerBound + 1 else { throw VideoStreamFailure.interrupted }
    }
}

/// How often a chunk is asked for before the player stops and says the Mac does not answer
/// (Pudge closed on the Mac: a few seconds; the Mac asleep: about half a minute).
public enum VideoStreamRetry {
    public static let attempts = 4
    /// Seconds a chunk may wait for the Mac's bytes before it counts as unanswered.
    public static let timeout: TimeInterval = 8
    public static func shouldRetry(_ failure: VideoStreamFailure, attempt: Int) -> Bool {
        attempt < attempts && failure.retryable
    }
    /// The first retry comes at once: a kept-alive connection the Mac closed while the video was
    /// paused fails once without the Mac being away.
    public static func delay(after attempt: Int) -> Double { attempt <= 1 ? 0.3 : min(4, Double(attempt)) }
    /// "Waiting for the Mac" shows from the second failed try of a chunk on.
    public static func showsWaiting(after attempt: Int) -> Bool { attempt >= 2 }
}

/// The Mac's stream of an episode (`POST /api/v2/media/episodes/<id>/stream`): the copy is kept
/// while the player renews it (`streams/<id>/keepalive`) and let go on `streams/<id>/close`.
public struct VideoStreamLease: Codable, Hashable, Sendable {
    public let id: String
    public let leaseSeconds: Double?
    public init(id: String, leaseSeconds: Double?) { self.id = id; self.leaseSeconds = leaseSeconds }

    /// Seconds between renewals: a third of the Mac's lease (a renewal or two may be lost).
    public var renewEvery: Double { max(10, min(60, (leaseSeconds ?? 90) / 3)) }
}

/// The answer to opening a stream: the episode's state as for prepare, `stream` once it is ready.
public struct VideoStreamOpened: Codable {
    public let state: String
    public let progress: Double?
    public let message: String?
    public let video: ResourceRef?
    public let durationMs: Int64?
    public let stream: VideoStreamLease?
}
