import Foundation

/// Which file a partial download on disk (`12.part`) is part of: written next to it as
/// `12.part.json` when the download starts. The bytes are kept when the connection is cut (Wi-Fi
/// drop, the Mac quitting for an update, Pudge killed) and the next attempt asks only for the rest.
public struct PartialDownload: Codable, Equatable, Sendable {
    public let sha256: String
    public let sizeBytes: Int64
    public init(sha256: String, sizeBytes: Int64) { self.sha256 = sha256; self.sizeBytes = sizeBytes }
}

/// Resuming a download of a file the Mac prepared with HTTP byte ranges (the Mac answers
/// `Range` with 206 and its ETag is the file's SHA-256, the same after a restart).
public enum RangedResume {
    /// What to do with the Mac's answer to a (resumed) request.
    public enum Decision: Equatable, Sendable {
        /// 206 from the requested offset: the body continues the partial file.
        case append
        /// 200 (the whole file; e.g. If-Range did not match): the partial file starts over.
        case restart
        /// 416 for an offset at the end: every byte is already here.
        case complete
        /// 206 for other bytes than asked for (another start, end or size, or no Content-Range):
        /// appended they would stitch the file wrong, so the partial goes and it starts from 0.
        case startOver
        /// Anything else: the Mac refused (its copy went or changed).
        case refuse
    }

    public static func markerURL(for partial: URL) -> URL { partial.appendingPathExtension("json") }

    /// Bytes of the partial file that can be kept: all of them when it belongs to the same file
    /// (`marker` equals `expected`) and is not longer than it; otherwise none (start over).
    public static func resumeOffset(marker: PartialDownload?, expected: PartialDownload, partialBytes: Int64?) -> Int64 {
        guard let marker, marker == expected, let bytes = partialBytes, bytes > 0, bytes <= expected.sizeBytes else { return 0 }
        return bytes
    }

    /// Headers of a request from `offset` (none from the start). If-Range: a Mac whose copy is
    /// another edition answers with the whole file instead of a tail of different bytes.
    public static func headers(offset: Int64, sha256: String) -> [String: String] {
        guard offset > 0 else { return [:] }
        return ["Range": "bytes=\(offset)-", "If-Range": "\"\(sha256)\""]
    }

    /// "bytes 100-999/1000" → (100, 999, 1000).
    public static func contentRange(_ value: String?) -> (start: Int64, end: Int64, total: Int64)? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        guard trimmed.lowercased().hasPrefix("bytes ") else { return nil }
        let spec = trimmed.dropFirst(6)
        let halves = spec.split(separator: "/", maxSplits: 1).map(String.init)
        guard halves.count == 2, let total = Int64(halves[1]) else { return nil }
        let bounds = halves[0].split(separator: "-", maxSplits: 1).map(String.init)
        guard bounds.count == 2, let start = Int64(bounds[0]), let end = Int64(bounds[1]), start <= end else { return nil }
        return (start, end, total)
    }

    public static func decide(status: Int, contentRange value: String?, offset: Int64, total: Int64) -> Decision {
        switch status {
        case 200:
            return .restart
        case 206:
            guard let range = contentRange(value), range.start == offset, range.total == total, range.end == total - 1 else { return .startOver }
            return .append
        case 416:
            return offset > 0 && offset == total ? .complete : .refuse
        default:
            return .refuse
        }
    }

    /// Whether a download whose resumed answer did not match goes on from 0 (once; a Mac that
    /// answers wrong again is refused).
    public static func startsOver(alreadyRestarted: Bool) -> Bool { !alreadyRestarted }

    /// The share shown while downloading: the bytes already here count from the start (a
    /// resumed download goes on at 60 %, not at 0 %).
    public static func fraction(received: Int64, total: Int64) -> Double {
        guard total > 0 else { return 0 }
        return min(1, max(0, Double(received) / Double(total)))
    }
}
