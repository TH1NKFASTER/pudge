import Foundation
import XCTest
@testable import PudgeCore

/// Watching from the Mac "like a video site": blocks ahead of the player are kept in a bounded
/// cache, a seek fetches at the new place at once, blocks already here are used again
/// (`swift test` in ios/PudgeCore on the Mac).
final class StreamCacheTests: XCTestCase {
    private var folder: URL!

    override func setUp() {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("stream-cache-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: folder)
    }

    /// The file's bytes (each one its offset mod 251): a slice in the wrong place shows.
    static func bytes(_ range: ClosedRange<Int64>) -> Data {
        Data(range.map { UInt8($0 % 251) })
    }

    // MARK: Pure parts

    func testBlocksCoverTheFileExactly() {
        XCTAssertEqual(StreamBlocks.count(fileSize: 0, blockSize: 10), 0)
        XCTAssertEqual(StreamBlocks.count(fileSize: 25, blockSize: 10), 3)
        XCTAssertEqual(StreamBlocks.range(0, fileSize: 25, blockSize: 10), 0...9)
        XCTAssertEqual(StreamBlocks.range(2, fileSize: 25, blockSize: 10), 20...24)
        XCTAssertNil(StreamBlocks.range(3, fileSize: 25, blockSize: 10))
        XCTAssertNil(StreamBlocks.range(-1, fileSize: 25, blockSize: 10))
        XCTAssertEqual(StreamBlocks.covering(0...1, fileSize: 25, blockSize: 10), [0])
        XCTAssertEqual(StreamBlocks.covering(9...20, fileSize: 25, blockSize: 10), [0, 1, 2])
        XCTAssertEqual(StreamBlocks.covering(5...24, fileSize: 25, blockSize: 10, limit: 2), [0, 1])
        XCTAssertEqual(StreamBlocks.covering(25...30, fileSize: 25, blockSize: 10), [])
    }

    func testOnlyTheWantedPartOfABlockIsHandedOver() {
        let block = Self.bytes(10...19)
        XCTAssertEqual(StreamBlocks.slice(block, block: 1, wanted: 13...15, blockSize: 10), Self.bytes(13...15))
        XCTAssertEqual(StreamBlocks.slice(block, block: 1, wanted: 0...100, blockSize: 10), block)
        XCTAssertEqual(StreamBlocks.slice(block, block: 1, wanted: 18...40, blockSize: 10), Self.bytes(18...19))
        XCTAssertNil(StreamBlocks.slice(block, block: 1, wanted: 0...9, blockSize: 10))
        // A slice of a slice keeps its own indices.
        let tail = block.subdata(in: 2..<10)
        XCTAssertEqual(StreamBlocks.slice(tail, block: 0, wanted: 3...4, blockSize: 8), tail.subdata(in: 3..<5))
    }

    func testReadAheadCoversTheNextMinutesWithinItsCap() {
        // 1 MB/s over 1 MB blocks: five minutes are 300 blocks ahead.
        let size: Int64 = 1500 * StreamBlocks.size
        XCTAssertEqual(StreamPrefetch.window(from: 0, fileSize: size, bytesPerSecond: Double(StreamBlocks.size), aheadSeconds: 300), 0...300)
        // A seek: the window starts at the new place.
        XCTAssertEqual(StreamPrefetch.window(from: 1000 * StreamBlocks.size + 5, fileSize: size, bytesPerSecond: Double(StreamBlocks.size),
                                             aheadSeconds: 300), 1000...1300)
        // Never past the end, never more than the cap.
        XCTAssertEqual(StreamPrefetch.window(from: 1400 * StreamBlocks.size, fileSize: size, bytesPerSecond: Double(StreamBlocks.size)), 1400...1499)
        XCTAssertEqual(StreamPrefetch.window(from: 0, fileSize: size, bytesPerSecond: 1e9, maxAhead: 10 * StreamBlocks.size), 0...10)
        XCTAssertNil(StreamPrefetch.window(from: 0, fileSize: 0, bytesPerSecond: 1))
        XCTAssertEqual(StreamPrefetch.bytesPerSecond(fileSize: 100, durationMs: nil), 1_000_000)
        // A 1.5 GB / 24 min episode: about 1 MB/s.
        XCTAssertEqual(StreamPrefetch.bytesPerSecond(fileSize: 1_440_000_000, durationMs: 1_440_000), 1_000_000, accuracy: 1)
    }

    func testTheNearestMissingBlocksComeFirstAndASeekLetsFarFetchesGo() {
        let have: Set<Int> = [10, 11, 13]
        XCTAssertEqual(StreamPrefetch.next(window: 10...20, have: { have.contains($0) }, limit: 2), [12, 14])
        XCTAssertEqual(StreamPrefetch.next(window: 10...11, have: { have.contains($0) }), [])
        XCTAssertTrue(StreamPrefetch.keeps(9, window: 10...20))
        XCTAssertTrue(StreamPrefetch.keeps(20, window: 10...20))
        XCTAssertFalse(StreamPrefetch.keeps(7, window: 10...20))
        XCTAssertFalse(StreamPrefetch.keeps(21, window: 10...20))
    }

    func testTheCacheLimitFollowsTheSettingAndTheFreeSpace() {
        let mb: Int64 = 1024 * 1024
        XCTAssertEqual(StreamCacheSetting.limitBytes(settingMB: 2048, freeBytes: nil), 2048 * mb)
        XCTAssertEqual(StreamCacheSetting.limitBytes(settingMB: 2048, freeBytes: 100_000 * mb), 2048 * mb)
        // 3 GB free: at most half of what is beyond the 1 GB kept free.
        XCTAssertEqual(StreamCacheSetting.limitBytes(settingMB: 2048, freeBytes: 3072 * mb), 1024 * mb)
        XCTAssertEqual(StreamCacheSetting.limitBytes(settingMB: 2048, freeBytes: 500 * mb), 64 * mb)
        let defaults = UserDefaults(suiteName: "stream-cache-\(UUID().uuidString)")!
        XCTAssertEqual(StreamCacheSetting.storedMB(defaults), StreamCacheSetting.defaultMB)
        defaults.set(512, forKey: StreamCacheSetting.defaultsKey)
        XCTAssertEqual(StreamCacheSetting.storedMB(defaults), 512)
    }

    // MARK: The cache on disk

    func testBlocksAreKeptAndUsedAgainAfterReopening() {
        let cache = StreamBlockCache(folder: folder, limitBytes: 1000)
        cache.store("sha-a", 0, Self.bytes(0...9))
        XCTAssertEqual(cache.data("sha-a", 0, length: 10), Self.bytes(0...9))
        XCTAssertTrue(cache.has("sha-a", 0))
        XCTAssertFalse(cache.has("sha-b", 0))
        // Another copy of the episode (another SHA) never gets these bytes.
        XCTAssertNil(cache.data("sha-b", 0, length: 10))
        // A block of the wrong length (damaged) goes.
        XCTAssertNil(cache.data("sha-a", 0, length: 11))
        XCTAssertFalse(cache.has("sha-a", 0))
        cache.store("sha-a", 3, Self.bytes(30...39))
        // The app opened again: what is on disk counts.
        let again = StreamBlockCache(folder: folder, limitBytes: 1000)
        XCTAssertEqual(again.totalBytes, 10)
        XCTAssertEqual(again.data("sha-a", 3, length: 10), Self.bytes(30...39))
        again.clear()
        XCTAssertEqual(again.totalBytes, 0)
        XCTAssertNil(again.data("sha-a", 3, length: 10))
    }

    func testTheLeastRecentlyUsedBlocksGoPastTheLimit() {
        let cache = StreamBlockCache(folder: folder, limitBytes: 30)
        cache.store("a", 0, Self.bytes(0...9))
        cache.store("a", 1, Self.bytes(10...19))
        cache.store("b", 0, Self.bytes(0...9))
        // Block a/0 used again: a/1 is now the oldest.
        XCTAssertNotNil(cache.data("a", 0, length: 10))
        cache.store("b", 1, Self.bytes(10...19))
        XCTAssertEqual(cache.totalBytes, 30)
        XCTAssertFalse(cache.has("a", 1))
        XCTAssertTrue(cache.has("a", 0))
        XCTAssertTrue(cache.has("b", 0))
        XCTAssertTrue(cache.has("b", 1))
        // A smaller limit (Settings): trimmed at once.
        cache.limitBytes = 10
        XCTAssertEqual(cache.totalBytes, 10)
        XCTAssertTrue(cache.has("b", 1))
        // A block bigger than the whole cache is not kept.
        cache.store("c", 0, Self.bytes(0...19))
        XCTAssertFalse(cache.has("c", 0))
    }

    // MARK: The player's blocks

    func testABlockWantedTwiceIsFetchedOnceAndCachedBlocksAreNotFetched() async throws {
        let mac = FakeMac(delay: 50_000_000)
        let cache = StreamBlockCache(folder: folder, limitBytes: 10_000)
        let source = StreamBlockSource(key: "sha", fileSize: 95, durationMs: 95_000, cache: cache, blockSize: 10,
                                       aheadSeconds: 0, fetch: mac.fetch)
        async let first = source.block(2)
        async let second = source.block(2)
        let (one, two) = try await (first, second)
        XCTAssertEqual(one, Self.bytes(20...29))
        XCTAssertEqual(two, one)
        XCTAssertEqual(mac.requests, [20...29])
        // The last block is short.
        let last = try await source.block(9)
        XCTAssertEqual(last, Self.bytes(90...94))
        // Opened again later (a new player): from the cache, nothing asked of the Mac.
        let reopened = StreamBlockSource(key: "sha", fileSize: 95, durationMs: 95_000, cache: StreamBlockCache(folder: folder, limitBytes: 10_000),
                                         blockSize: 10, aheadSeconds: 0, fetch: mac.fetch)
        let cached = try await reopened.block(2)
        XCTAssertEqual(cached, Self.bytes(20...29))
        XCTAssertEqual(mac.requests.count, 2)
    }

    func testASeekStopsFetchesOfTheOldPlaceAndFetchesTheNewOneAtOnce() async throws {
        let mac = FakeMac(delay: 2_000_000_000)
        let cache = StreamBlockCache(folder: folder, limitBytes: 1_000_000)
        let source = StreamBlockSource(key: "sha", fileSize: 10_000, durationMs: 1_000_000, cache: cache, blockSize: 10,
                                       aheadSeconds: 0, fetch: mac.fetch)
        source.moved(to: 0)
        source.warm([0, 1])
        XCTAssertEqual(source.fetching, [0, 1])
        // The player seeks to the middle: the slow fetches of the start stop.
        source.moved(to: 5000)
        XCTAssertEqual(source.fetching, [])
        let fast = FakeMac(delay: 0)
        let seeked = StreamBlockSource(key: "sha", fileSize: 10_000, durationMs: 1_000_000, cache: cache, blockSize: 10,
                                       aheadSeconds: 0, fetch: fast.fetch)
        seeked.moved(to: 5000)
        let data = try await seeked.block(500)
        XCTAssertEqual(data, Self.bytes(5000...5009))
        XCTAssertEqual(fast.requests.first, 5000...5009)
        source.stop()
    }

    func testARequestWhoseBlockAnotherSeekStoppedStillGetsItsBytes() async throws {
        let mac = FakeMac(delay: 300_000_000)
        let source = StreamBlockSource(key: "sha", fileSize: 10_000, durationMs: 1_000_000,
                                       cache: StreamBlockCache(folder: folder, limitBytes: 1_000_000), blockSize: 10,
                                       aheadSeconds: 0, fetch: mac.fetch)
        source.moved(to: 0)
        async let wanted = source.block(0)
        try await Task.sleep(nanoseconds: 50_000_000)
        // Another request far away moved the read place meanwhile: block 0 is fetched again.
        source.moved(to: 9000)
        let data = try await wanted
        XCTAssertEqual(data, Self.bytes(0...9))
        XCTAssertEqual(mac.requests.filter { $0 == 0...9 }.count, 2)
    }

    func testTheReadAheadFillsTheCacheAheadOfThePlayerOnly() async throws {
        let mac = FakeMac(delay: 0)
        let cache = StreamBlockCache(folder: folder, limitBytes: 1_000_000)
        // 10 bytes a second, 5 s ahead: blocks 50...55 from byte 500.
        let source = StreamBlockSource(key: "sha", fileSize: 1000, durationMs: 100_000, cache: cache, blockSize: 10,
                                       aheadSeconds: 5, fetch: mac.fetch)
        source.moved(to: 500)
        source.startReadAhead()
        let deadline = Date().addingTimeInterval(5)
        while !(50...55).allSatisfy({ cache.has("sha", $0) }) && Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        source.stop()
        XCTAssertTrue((50...55).allSatisfy { cache.has("sha", $0) })
        XCTAssertFalse(cache.has("sha", 49))
        XCTAssertFalse(cache.has("sha", 57))
        // Nothing behind the player was asked for.
        XCTAssertEqual(mac.requests.map(\.lowerBound).min(), 500)
    }

    func testAfterStopNothingIsFetched() async {
        let mac = FakeMac(delay: 0)
        let source = StreamBlockSource(key: "sha", fileSize: 100, durationMs: 10_000,
                                       cache: StreamBlockCache(folder: folder, limitBytes: 1000), blockSize: 10, fetch: mac.fetch)
        source.stop()
        do {
            _ = try await source.block(0)
            XCTFail("a stopped source fetched")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        XCTAssertEqual(mac.requests, [])
    }
}

/// The Mac's answers to byte ranges, after `delay` nanoseconds (cancellable).
final class FakeMac: @unchecked Sendable {
    private let lock = NSLock()
    private var asked: [ClosedRange<Int64>] = []
    let delay: UInt64
    init(delay: UInt64) { self.delay = delay }

    var requests: [ClosedRange<Int64>] { lock.lock(); defer { lock.unlock() }; return asked }
    private func record(_ range: ClosedRange<Int64>) { lock.lock(); asked.append(range); lock.unlock() }

    var fetch: StreamBlockSource.Fetch {
        { [self] range in
            record(range)
            if delay > 0 { try await Task.sleep(nanoseconds: delay) }
            return StreamCacheTests.bytes(range)
        }
    }
}
