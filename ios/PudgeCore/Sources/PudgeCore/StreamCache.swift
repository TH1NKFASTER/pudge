import Foundation

/// Watching from the Mac like a video site: the part of the episode ahead of where it plays is
/// fetched in blocks and kept on disk (a bounded cache, the least recently used blocks go
/// first), so a short Wi-Fi drop does not stop the video and a huge episode never needs its
/// whole size on the device. A seek fetches at the new place at once (fetches for the old one
/// stop); blocks already here are used again, also when the episode is opened again later.

/// The file in fixed blocks (the last one shorter). Block `n` is bytes `n*size ..< (n+1)*size`.
public enum StreamBlocks {
    /// 1 MiB: the first one (and the first after a seek) comes in about a tenth of a second
    /// over the local network.
    public static let size: Int64 = 1024 * 1024

    public static func index(of offset: Int64, blockSize: Int64 = size) -> Int {
        Int(max(0, offset) / max(1, blockSize))
    }

    public static func count(fileSize: Int64, blockSize: Int64 = size) -> Int {
        guard fileSize > 0 else { return 0 }
        return Int((fileSize + max(1, blockSize) - 1) / max(1, blockSize))
    }

    /// The bytes of block `index` (nil: past the end).
    public static func range(_ index: Int, fileSize: Int64, blockSize: Int64 = size) -> ClosedRange<Int64>? {
        let start = Int64(index) * max(1, blockSize)
        guard index >= 0, start < fileSize else { return nil }
        return start...min(fileSize - 1, start + max(1, blockSize) - 1)
    }

    /// The blocks a byte range needs, in order (`limit` at most, from its start).
    public static func covering(_ range: ClosedRange<Int64>, fileSize: Int64, blockSize: Int64 = size,
                                limit: Int = .max) -> [Int] {
        guard fileSize > 0, range.lowerBound < fileSize else { return [] }
        let first = index(of: range.lowerBound, blockSize: blockSize)
        let last = index(of: min(range.upperBound, fileSize - 1), blockSize: blockSize)
        return Array(first...max(first, min(last, first + max(1, limit) - 1)))
    }

    /// The part of block `index` (its `data`) that falls inside `wanted` (nil: none of it).
    public static func slice(_ data: Data, block index: Int, wanted: ClosedRange<Int64>,
                             blockSize: Int64 = size) -> Data? {
        let start = Int64(index) * max(1, blockSize)
        guard !data.isEmpty else { return nil }
        let end = start + Int64(data.count) - 1
        let low = max(start, wanted.lowerBound), high = min(end, wanted.upperBound)
        guard low <= high else { return nil }
        let from = data.startIndex + Int(low - start), to = data.startIndex + Int(high - start) + 1
        return data.subdata(in: from..<to)
    }
}

/// What to fetch ahead of the place the player reads at.
public enum StreamPrefetch {
    /// Seconds of video kept ahead of the player (about five minutes, as a video site does on Wi-Fi).
    public static let aheadSeconds: Double = 300
    /// Never more than this ahead, whatever the bit rate.
    public static let maxAheadBytes: Int64 = 600 * 1024 * 1024
    /// Blocks fetched ahead at the same time (the player's own blocks come first, apart).
    public static let parallel = 2
    /// Blocks just behind the read place that a seek keeps fetching (AVPlayer reads a little back).
    public static let behind = 2

    /// Bytes per second of the episode (nil duration: assumed 1 MB/s, a 1080p episode's rate).
    public static func bytesPerSecond(fileSize: Int64, durationMs: Int64?) -> Double {
        guard let durationMs, durationMs > 0, fileSize > 0 else { return 1_000_000 }
        return Double(fileSize) / (Double(durationMs) / 1000)
    }

    /// The blocks the read-ahead covers from `offset` (both ends included).
    public static func window(from offset: Int64, fileSize: Int64, bytesPerSecond: Double,
                              aheadSeconds: Double = aheadSeconds, maxAhead: Int64 = maxAheadBytes,
                              blockSize: Int64 = StreamBlocks.size) -> ClosedRange<Int>? {
        let count = StreamBlocks.count(fileSize: fileSize, blockSize: blockSize)
        guard count > 0 else { return nil }
        let first = min(count - 1, StreamBlocks.index(of: offset, blockSize: blockSize))
        let ahead = min(Double(max(0, maxAhead)), max(0, bytesPerSecond) * max(0, aheadSeconds))
        let last = min(count - 1, StreamBlocks.index(of: offset + Int64(ahead), blockSize: blockSize))
        return first...max(first, last)
    }

    /// The next blocks to fetch ahead: missing from the window, nearest first (`limit` at most).
    public static func next(window: ClosedRange<Int>, have: (Int) -> Bool, limit: Int = parallel) -> [Int] {
        var result: [Int] = []
        for index in window where !have(index) {
            result.append(index)
            if result.count >= limit { break }
        }
        return result
    }

    /// A fetch still worth finishing after the player moved to `window` (a seek elsewhere stops it).
    public static func keeps(_ index: Int, window: ClosedRange<Int>) -> Bool {
        index >= window.lowerBound - behind && index <= window.upperBound
    }
}

/// Settings → Downloads → "Watch cache": how much of the device the blocks may take.
public enum StreamCacheSetting {
    public static let defaultsKey = "pudge.streamCacheMB"
    public static let defaultMB = 2048
    /// The choices offered (MB).
    public static let choices = [512, 1024, 2048, 5120]
    /// Space always left free on the device (downloads, photos, the system).
    public static let keepFree: Int64 = 1024 * 1024 * 1024

    public static func storedMB(_ defaults: UserDefaults = .standard) -> Int {
        let value = defaults.integer(forKey: defaultsKey)
        return value > 0 ? value : defaultMB
    }

    /// The limit: the setting, but at most half of what is free beyond `keepFree` (`freeBytes`
    /// counts the cache's own blocks as free; nil: not known). Never below 64 MB (a few
    /// minutes ahead stay possible on a full device).
    public static func limitBytes(settingMB: Int, freeBytes: Int64?) -> Int64 {
        let wanted = Int64(max(1, settingMB)) * 1024 * 1024
        guard let freeBytes else { return wanted }
        return max(64 * 1024 * 1024, min(wanted, max(0, freeBytes - keepFree) / 2))
    }
}

/// Blocks of episodes on disk (`<folder>/<file key>/<index>.blk`), within `limitBytes` in all: the
/// least recently used go first. Thread-safe.
public final class StreamBlockCache: @unchecked Sendable {
    public let folder: URL
    private let lock = NSLock()
    private var limit: Int64
    private var entries: [String: Entry] = [:]
    private var total: Int64 = 0
    private var loaded = false
    private var clock: Double = 0

    private struct Entry { var bytes: Int64; var used: Double }

    public init(folder: URL, limitBytes: Int64) {
        self.folder = folder; self.limit = max(0, limitBytes)
    }

    public var limitBytes: Int64 {
        get { locked { limit } }
        set { locked { limit = max(0, newValue) }; trim() }
    }

    /// Bytes kept now.
    public var totalBytes: Int64 { load(); return locked { total } }

    /// The block when it is here with the length it must have (a damaged one goes).
    public func data(_ key: String, _ index: Int, length: Int64) -> Data? {
        load()
        let url = blockURL(key, index)
        let name = entryName(key, index)
        guard locked({ entries[name] != nil }) else { return nil }
        guard let data = try? Data(contentsOf: url), Int64(data.count) == length else {
            remove(name, url)
            return nil
        }
        let now = Date().timeIntervalSince1970
        locked { entries[name]?.used = max(now, nextClock(now)) }
        // The order survives a restart of the app.
        try? FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: now)], ofItemAtPath: url.path)
        return data
    }

    public func has(_ key: String, _ index: Int) -> Bool {
        load()
        return locked { entries[entryName(key, index)] != nil }
    }

    /// Keeps a block (written whole or not at all); older blocks go past the limit.
    public func store(_ key: String, _ index: Int, _ data: Data) {
        load()
        guard !data.isEmpty, Int64(data.count) <= locked({ limit }) else { return }
        let url = blockURL(key, index)
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
        } catch { return }
        let now = Date().timeIntervalSince1970
        locked {
            let name = entryName(key, index)
            total -= entries[name]?.bytes ?? 0
            entries[name] = Entry(bytes: Int64(data.count), used: max(now, nextClock(now)))
            total += Int64(data.count)
        }
        trim()
    }

    /// Everything goes (Settings).
    public func clear() {
        locked { entries = [:]; total = 0; loaded = true }
        try? FileManager.default.removeItem(at: folder)
    }

    // MARK: Inside

    private func trim() {
        let victims: [(String, URL)] = locked {
            guard total > limit else { return [] }
            var result: [(String, URL)] = []
            for (name, entry) in entries.sorted(by: { $0.value.used < $1.value.used }) {
                if total <= limit { break }
                entries[name] = nil
                total -= entry.bytes
                result.append((name, folder.appendingPathComponent(name)))
            }
            return result
        }
        for (_, url) in victims { try? FileManager.default.removeItem(at: url) }
    }

    private func remove(_ name: String, _ url: URL) {
        locked {
            if let entry = entries.removeValue(forKey: name) { total -= entry.bytes }
        }
        try? FileManager.default.removeItem(at: url)
    }

    /// The blocks on disk, read once (their modification time is their last use).
    private func load() {
        guard !locked({ loaded }) else { return }
        var found: [String: Entry] = [:]
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey]
        if let files = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: keys) {
            for case let url as URL in files where url.pathExtension == "blk" {
                guard let values = try? url.resourceValues(forKeys: Set(keys)), values.isRegularFile == true,
                      let size = values.fileSize else { continue }
                let name = url.deletingLastPathComponent().lastPathComponent + "/" + url.lastPathComponent
                found[name] = Entry(bytes: Int64(size), used: values.contentModificationDate?.timeIntervalSince1970 ?? 0)
            }
        }
        locked {
            guard !loaded else { return }
            loaded = true
            for (name, entry) in found where entries[name] == nil {
                entries[name] = entry
                total += entry.bytes
            }
        }
        trim()
    }

    /// Uses within the same instant keep their order.
    private func nextClock(_ now: Double) -> Double {
        clock = max(clock + 0.000_001, now)
        return clock
    }

    private func entryName(_ key: String, _ index: Int) -> String { Self.safe(key) + "/\(index).blk" }
    private func blockURL(_ key: String, _ index: Int) -> URL { folder.appendingPathComponent(entryName(key, index)) }

    static func safe(_ key: String) -> String {
        let value = String(key.filter { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-") }.prefix(80))
        return value.isEmpty ? "file" : value
    }

    @discardableResult
    private func locked<T>(_ body: () -> T) -> T {
        lock.lock(); defer { lock.unlock() }
        return body()
    }
}

/// The blocks of one episode for the player: from the cache, or fetched once (a block the player
/// and the read-ahead both want is asked for once). `moved(to:)` names where the player reads; the
/// read-ahead keeps the next minutes in the cache, and a seek stops fetches of the old place.
public final class StreamBlockSource: @unchecked Sendable {
    public typealias Fetch = @Sendable (ClosedRange<Int64>) async throws -> Data

    public let key: String
    public let fileSize: Int64
    public let blockSize: Int64
    private let cache: StreamBlockCache?
    private let fetch: Fetch
    /// The part of a download of this copy already on the device (play while downloading).
    private let local: PartialFileShare?
    private let bytesPerSecond: Double
    private let aheadSeconds: Double
    private let maxAhead: Int64
    private let lock = NSLock()
    private var inflight: [Int: (id: Int, task: Task<Data, Error>)] = [:]
    private var serial = 0
    private var anchor: Int64 = 0
    private var prefetcher: Task<Void, Never>?
    private var stopped = false

    /// `key`: the copy's SHA-256 (another copy of the episode never mixes with this one).
    public init(key: String, fileSize: Int64, durationMs: Int64?, cache: StreamBlockCache?,
                blockSize: Int64 = StreamBlocks.size, aheadSeconds: Double = StreamPrefetch.aheadSeconds,
                maxAheadBytes: Int64 = StreamPrefetch.maxAheadBytes, local: PartialFileShare? = nil, fetch: @escaping Fetch) {
        self.key = key; self.fileSize = fileSize; self.blockSize = max(1, blockSize); self.cache = cache; self.fetch = fetch
        self.local = local
        bytesPerSecond = StreamPrefetch.bytesPerSecond(fileSize: fileSize, durationMs: durationMs)
        self.aheadSeconds = aheadSeconds
        // Never more ahead than half the cache: what was fetched ahead must not push itself out.
        self.maxAhead = min(maxAheadBytes, (cache?.limitBytes ?? 0) / 2)
    }

    /// Where the player reads now: the read-ahead goes on from here, fetches of blocks a seek
    /// left behind stop (their bandwidth goes to the new place).
    public func moved(to offset: Int64) {
        let stale: [Task<Data, Error>] = locked {
            anchor = max(0, offset)
            guard let window = windowLocked() else { return [] }
            var result: [Task<Data, Error>] = []
            for (index, entry) in inflight where !StreamPrefetch.keeps(index, window: window) {
                inflight[index] = nil
                result.append(entry.task)
            }
            return result
        }
        stale.forEach { $0.cancel() }
    }

    /// The block's bytes (downloaded here already, cached, joined to a fetch on its way, or fetched now).
    public func block(_ index: Int) async throws -> Data {
        guard let range = StreamBlocks.range(index, fileSize: fileSize, blockSize: blockSize) else {
            throw VideoStreamFailure.changed
        }
        let length = range.upperBound - range.lowerBound + 1
        while true {
            try Task.checkCancellation()
            if let data = local?.read(key, range) { return data }
            if let data = cache?.data(key, index, length: length) { return data }
            let task = try start(index, range)
            do {
                return try await task.value
            } catch is CancellationError {
                // The fetch was stopped by a seek elsewhere while this request still wants it: again.
                try Task.checkCancellation()
                if isStopped { throw CancellationError() }
            }
        }
    }

    /// Starts fetching blocks the player will want next (no waiting).
    public func warm(_ indices: [Int]) {
        for index in indices {
            guard let range = StreamBlocks.range(index, fileSize: fileSize, blockSize: blockSize),
                  !(cache?.has(key, index) ?? false), !(local?.covers(key, range) ?? false) else { continue }
            _ = try? start(index, range)
        }
    }

    /// The read-ahead runs until `stop()` (only with a cache: without one there is nowhere to keep it).
    public func startReadAhead() {
        guard cache != nil, maxAhead > 0 else { return }
        locked {
            guard prefetcher == nil, !stopped else { return }
            prefetcher = Task { [weak self] in await self?.readAhead() }
        }
    }

    /// The player closed: every fetch stops.
    public func stop() {
        let (tasks, reader): ([Task<Data, Error>], Task<Void, Never>?) = locked {
            defer { stopped = true; inflight = [:]; prefetcher = nil }
            return (inflight.values.map { $0.task }, prefetcher)
        }
        tasks.forEach { $0.cancel() }
        reader?.cancel()
    }

    /// The bytes here without asking the Mac (the download's part, the cached blocks), joined:
    /// the position bar's lighter track.
    public func availableBytes() -> [ClosedRange<Int64>] {
        let written = min(fileSize, local?.validBytes(key) ?? 0)
        var ranges: [ClosedRange<Int64>] = cache.map { cache in
            ByteRanges.blocks(fileSize: fileSize, blockSize: blockSize) { cache.has(key, $0) }
        } ?? []
        if written > 0 { ranges.append(0...(written - 1)) }
        return ByteRanges.merge(ranges)
    }

    /// Blocks on their way now (tests, logs).
    public var fetching: [Int] { locked { inflight.keys.sorted() } }

    // MARK: Inside

    private var isStopped: Bool { locked { stopped } }

    private func start(_ index: Int, _ range: ClosedRange<Int64>) throws -> Task<Data, Error> {
        try locked { () throws -> Task<Data, Error> in
            if stopped { throw CancellationError() }
            if let entry = inflight[index] { return entry.task }
            serial += 1
            let id = serial
            let fetch = self.fetch, cache = self.cache, key = self.key
            let task = Task<Data, Error> { [weak self] in
                defer { self?.finished(index, id) }
                let data = try await fetch(range)
                try Task.checkCancellation()
                cache?.store(key, index, data)
                return data
            }
            inflight[index] = (id: id, task: task)
            return task
        }
    }

    /// The block is in the part of the download already here (not fetched ahead again).
    private func downloaded(_ index: Int) -> Bool {
        guard let local, let range = StreamBlocks.range(index, fileSize: fileSize, blockSize: blockSize) else { return false }
        return local.covers(key, range)
    }

    private func finished(_ index: Int, _ id: Int) {
        locked { if inflight[index]?.id == id { inflight[index] = nil } }
    }

    private func windowLocked() -> ClosedRange<Int>? {
        StreamPrefetch.window(from: anchor, fileSize: fileSize, bytesPerSecond: bytesPerSecond,
                              aheadSeconds: aheadSeconds, maxAhead: maxAhead, blockSize: blockSize)
    }

    /// One block ahead into the cache. A seek stops it for good (not fetched again, unlike a
    /// block the player waits for). True: it failed (the Mac does not answer).
    private func prefetch(_ index: Int) async -> Bool {
        guard let range = StreamBlocks.range(index, fileSize: fileSize, blockSize: blockSize) else { return false }
        do {
            _ = try await start(index, range).value
            return false
        } catch {
            return !(error is CancellationError)
        }
    }

    private func readAhead() async {
        while !Task.isCancelled {
            let next: [Int] = locked {
                guard !stopped, let window = windowLocked() else { return [] }
                let busy = inflight.count
                guard busy < StreamPrefetch.parallel else { return [] }
                return StreamPrefetch.next(window: window, have: { index in
                    self.inflight[index] != nil || (self.cache?.has(self.key, index) ?? true)
                        || self.downloaded(index)
                }, limit: StreamPrefetch.parallel - busy)
            }
            if isStopped { return }
            if next.isEmpty {
                try? await Task.sleep(nanoseconds: 250_000_000)
                continue
            }
            let failed: Bool = await withTaskGroup(of: Bool.self) { group in
                for index in next {
                    group.addTask { [weak self] in
                        guard let self else { return false }
                        return await self.prefetch(index)
                    }
                }
                var any = false
                for await value in group { any = any || value }
                return any
            }
            // The Mac does not answer: the player's own requests say so; the read-ahead waits a little.
            if failed { try? await Task.sleep(nanoseconds: 2_000_000_000) }
        }
    }

    @discardableResult
    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock(); defer { lock.unlock() }
        return try body()
    }
}
