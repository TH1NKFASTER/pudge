import Foundation

/// The player's position bar shows what is already here, as on a video site: a lighter track
/// behind the played part where no waiting is needed (AVPlayer's own buffer, the blocks of the
/// watch cache, the part of a download already on this device), updated while it plays.
public enum BufferedBar {
    /// Two parts closer than this (share of the bar) are drawn as one.
    public static let joinGap = 0.002

    /// Parts of the bar (shares 0...1) in order, overlapping or touching ones joined, empty or
    /// invalid ones dropped, everything kept within 0...1.
    public static func merge(_ parts: [ClosedRange<Double>], gap: Double = joinGap) -> [ClosedRange<Double>] {
        let clean = parts.compactMap { part -> ClosedRange<Double>? in
            guard part.lowerBound.isFinite, part.upperBound.isFinite else { return nil }
            let low = min(1, max(0, part.lowerBound)), high = min(1, max(0, part.upperBound))
            return high > low ? low...high : nil
        }.sorted { $0.lowerBound < $1.lowerBound }
        var result: [ClosedRange<Double>] = []
        for part in clean {
            if let last = result.last, part.lowerBound <= last.upperBound + max(0, gap) {
                result[result.count - 1] = last.lowerBound...max(last.upperBound, part.upperBound)
            } else {
                result.append(part)
            }
        }
        return result
    }

    /// AVPlayerItem.loadedTimeRanges (start and duration in seconds) as parts of the bar.
    public static func fromTimes(_ ranges: [(start: Double, duration: Double)], durationSeconds: Double) -> [ClosedRange<Double>] {
        guard durationSeconds.isFinite, durationSeconds > 0 else { return [] }
        return merge(ranges.compactMap { range in
            guard range.start.isFinite, range.duration.isFinite, range.duration > 0 else { return nil }
            return (range.start / durationSeconds)...((range.start + range.duration) / durationSeconds)
        })
    }

    /// Byte ranges of the file (both ends included) as parts of the bar: bytes map to time in
    /// proportion (an episode's bit rate is about even; the index at its start is small).
    public static func fromBytes(_ ranges: [ClosedRange<Int64>], fileSize: Int64) -> [ClosedRange<Double>] {
        guard fileSize > 0 else { return [] }
        let size = Double(fileSize)
        return merge(ranges.compactMap { range -> ClosedRange<Double>? in
            let low = max(0, range.lowerBound), high = min(fileSize, range.upperBound + 1)
            return high > low ? (Double(low) / size)...(Double(high) / size) : nil
        })
    }

    /// The share of the bar under a touch at `x` of a bar `width` wide.
    public static func fraction(x: Double, width: Double) -> Double {
        guard width > 0, x.isFinite else { return 0 }
        return min(1, max(0, x / width))
    }
}

/// Byte ranges of a file (both ends included), joined.
public enum ByteRanges {
    public static func merge(_ ranges: [ClosedRange<Int64>]) -> [ClosedRange<Int64>] {
        var result: [ClosedRange<Int64>] = []
        for range in ranges.sorted(by: { $0.lowerBound < $1.lowerBound }) {
            if let last = result.last, range.lowerBound <= last.upperBound + 1 {
                result[result.count - 1] = last.lowerBound...max(last.upperBound, range.upperBound)
            } else {
                result.append(range)
            }
        }
        return result
    }

    /// The bytes of the blocks that are here (`has`), joined.
    public static func blocks(fileSize: Int64, blockSize: Int64 = StreamBlocks.size, has: (Int) -> Bool) -> [ClosedRange<Int64>] {
        var result: [ClosedRange<Int64>] = []
        for index in 0..<StreamBlocks.count(fileSize: fileSize, blockSize: blockSize) where has(index) {
            if let range = StreamBlocks.range(index, fileSize: fileSize, blockSize: blockSize) { result.append(range) }
        }
        return merge(result)
    }
}

/// Play while downloading: an episode download in progress writes the file from its start, in
/// order; the player watching the same copy (its SHA-256) takes the bytes already written from
/// that file and only the rest from the Mac. The downloader says what is written (`advance`)
/// and when the file starts over (`reset`, before it is cut); a read holds the lock, so the
/// file is never cut under it. Thread-safe.
public final class PartialFileShare: @unchecked Sendable {
    public static let shared = PartialFileShare()

    private struct Entry { var url: URL; var valid: Int64 }
    private let lock = NSLock()
    private var entries: [String: Entry] = [:]

    public init() {}

    /// The download of `key` writes `url`; its first `bytes` are written (a resumed download's
    /// bytes already here, then each write).
    public func advance(_ key: String, url: URL, to bytes: Int64) {
        locked { entries[key] = Entry(url: url, valid: max(0, bytes)) }
    }

    /// The file is about to start over (or be cut): none of its bytes count until written again.
    /// Waits for a read in progress.
    public func reset(_ key: String, url: URL) {
        locked { entries[key] = Entry(url: url, valid: 0) }
    }

    /// The finished download moved to its place, verified: all of it is there.
    public func moved(_ key: String, to url: URL, sizeBytes: Int64) {
        locked { entries[key] = Entry(url: url, valid: max(0, sizeBytes)) }
    }

    /// The file went (cancelled, deleted): nothing is read from it any more.
    public func forget(url: URL) {
        locked {
            for (key, entry) in entries where entry.url.standardizedFileURL == url.standardizedFileURL { entries[key] = nil }
        }
    }

    /// Bytes written from the start (0: none, or no download of this copy).
    public func validBytes(_ key: String) -> Int64 { locked { entries[key]?.valid ?? 0 } }

    /// Every byte of `range` is written.
    public func covers(_ key: String, _ range: ClosedRange<Int64>) -> Bool {
        locked { (entries[key]?.valid ?? 0) > range.upperBound && range.lowerBound >= 0 }
    }

    /// The bytes of `range` from the file, when all of them are written (nil otherwise, or when
    /// the file cannot be read: the Mac sends them instead).
    public func read(_ key: String, _ range: ClosedRange<Int64>) -> Data? {
        locked { () -> Data? in
            guard let entry = entries[key], range.lowerBound >= 0, entry.valid > range.upperBound else { return nil }
            guard let handle = try? FileHandle(forReadingFrom: entry.url) else { return nil }
            defer { try? handle.close() }
            let count = Int(range.upperBound - range.lowerBound + 1)
            guard (try? handle.seek(toOffset: UInt64(range.lowerBound))) != nil,
                  let data = try? handle.read(upToCount: count), data.count == count else { return nil }
            return data
        }
    }

    @discardableResult
    private func locked<T>(_ body: () -> T) -> T {
        lock.lock(); defer { lock.unlock() }
        return body()
    }
}
