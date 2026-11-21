import Foundation
import XCTest
@testable import PudgeCore

/// The position bar's lighter track (what is already here) and play while downloading (the
/// player reads the part of a download already written, the rest comes from the Mac).
/// `swift test` in ios/PudgeCore on the Mac.
final class PlaybackBufferTests: XCTestCase {
    private var folder: URL!

    override func setUp() {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("playback-buffer-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: folder)
    }

    // MARK: The bar

    func testPartsAreJoinedSortedAndKeptOnTheBar() {
        XCTAssertEqual(BufferedBar.merge([]), [])
        XCTAssertEqual(BufferedBar.merge([0.5...0.6, 0.1...0.2, 0.15...0.3]), [0.1...0.3, 0.5...0.6])
        // Touching (within the gap) parts are one; empty and off-bar ones go.
        XCTAssertEqual(BufferedBar.merge([0.1...0.2, 0.201...0.4], gap: 0.002), [0.1...0.4])
        XCTAssertEqual(BufferedBar.merge([0.3...0.3, 1.2...1.5, -0.5...0.1]), [0...0.1])
        XCTAssertEqual(BufferedBar.merge([0.9...1.4]), [0.9...1])
        XCTAssertEqual(BufferedBar.merge([0...Double.infinity]), [])
    }

    func testPlayerTimeRangesMapToTheBar() {
        let parts = BufferedBar.fromTimes([(start: 60, duration: 30), (start: 0, duration: 10), (start: 85, duration: 30)],
                                          durationSeconds: 1200)
        XCTAssertEqual(parts.count, 2)
        XCTAssertEqual(parts[0].lowerBound, 0, accuracy: 1e-9)
        XCTAssertEqual(parts[0].upperBound, 10.0 / 1200, accuracy: 1e-9)
        XCTAssertEqual(parts[1].lowerBound, 60.0 / 1200, accuracy: 1e-9)
        XCTAssertEqual(parts[1].upperBound, 115.0 / 1200, accuracy: 1e-9)
        // Duration not known yet, or a range AVPlayer reports as invalid: nothing.
        XCTAssertEqual(BufferedBar.fromTimes([(start: 0, duration: 10)], durationSeconds: 0), [])
        XCTAssertEqual(BufferedBar.fromTimes([(start: Double.nan, duration: 10), (start: 5, duration: 0)], durationSeconds: 100), [])
    }

    func testByteRangesMapToTheBarInProportion() {
        XCTAssertEqual(BufferedBar.fromBytes([0...249], fileSize: 1000), [0...0.25])
        XCTAssertEqual(BufferedBar.fromBytes([500...999, 0...99], fileSize: 1000), [0...0.1, 0.5...1])
        // Past the end, negative or nothing: within the bar or dropped.
        XCTAssertEqual(BufferedBar.fromBytes([900...5000], fileSize: 1000), [0.9...1])
        XCTAssertEqual(BufferedBar.fromBytes([-10...(-2)], fileSize: 1000), [])
        XCTAssertEqual(BufferedBar.fromBytes([0...10], fileSize: 0), [])
    }

    func testATouchMapsToAShareOfTheBar() {
        XCTAssertEqual(BufferedBar.fraction(x: 50, width: 200), 0.25)
        XCTAssertEqual(BufferedBar.fraction(x: -5, width: 200), 0)
        XCTAssertEqual(BufferedBar.fraction(x: 500, width: 200), 1)
        XCTAssertEqual(BufferedBar.fraction(x: 5, width: 0), 0)
    }

    func testCachedBlocksBecomeJoinedByteRanges() {
        let have: Set<Int> = [0, 1, 2, 5, 9]
        XCTAssertEqual(ByteRanges.blocks(fileSize: 95, blockSize: 10) { have.contains($0) }, [0...29, 50...59, 90...94])
        XCTAssertEqual(ByteRanges.merge([10...19, 0...9, 30...40, 35...38]), [0...19, 30...40])
        XCTAssertEqual(ByteRanges.blocks(fileSize: 0, blockSize: 10) { _ in true }, [])
    }

    // MARK: Play while downloading

    private func file(_ bytes: ClosedRange<Int64>) throws -> URL {
        let url = folder.appendingPathComponent("1.part")
        try StreamCacheTests.bytes(bytes).write(to: url)
        return url
    }

    func testOnlyBytesTheDownloadWroteAreRead() throws {
        let share = PartialFileShare()
        let url = try file(0...99)
        XCTAssertNil(share.read("sha", 0...9), "no download of this copy")
        share.advance("sha", url: url, to: 50)
        XCTAssertEqual(share.read("sha", 10...19), StreamCacheTests.bytes(10...19))
        XCTAssertEqual(share.read("sha", 0...49), StreamCacheTests.bytes(0...49))
        XCTAssertNil(share.read("sha", 40...50), "byte 50 is not written yet")
        XCTAssertNil(share.read("other", 0...9), "another copy never reads this file")
        XCTAssertTrue(share.covers("sha", 0...49))
        XCTAssertFalse(share.covers("sha", 0...50))
        XCTAssertEqual(share.validBytes("sha"), 50)
    }

    func testAFileStartingOverOrGoneIsNotRead() throws {
        let share = PartialFileShare()
        let url = try file(0...99)
        share.advance("sha", url: url, to: 100)
        share.reset("sha", url: url)
        XCTAssertNil(share.read("sha", 0...9), "the file starts over: its old bytes are not used")
        XCTAssertEqual(share.validBytes("sha"), 0)
        share.advance("sha", url: url, to: 100)
        share.forget(url: url)
        XCTAssertNil(share.read("sha", 0...9))
        // Written as said but shorter on disk (removed under it): nothing, never short data.
        share.advance("sha", url: url, to: 100)
        try FileManager.default.removeItem(at: url)
        XCTAssertNil(share.read("sha", 0...9))
    }

    func testTheFinishedDownloadIsReadFromItsPlace() throws {
        let share = PartialFileShare()
        let partial = try file(0...99)
        share.advance("sha", url: partial, to: 60)
        let target = folder.appendingPathComponent("1.mp4")
        try FileManager.default.moveItem(at: partial, to: target)
        share.moved("sha", to: target, sizeBytes: 100)
        // The partial file's leftovers going later do not touch it.
        share.forget(url: partial)
        XCTAssertEqual(share.read("sha", 90...99), StreamCacheTests.bytes(90...99))
    }

    func testThePlayerTakesTheDownloadedPartAndOnlyTheRestFromTheMac() async throws {
        let share = PartialFileShare()
        let url = try file(0...24)
        share.advance("sha", url: url, to: 20)
        let asked = AskedRanges()
        let source = StreamBlockSource(key: "sha", fileSize: 50, durationMs: nil, cache: nil, blockSize: 10, local: share) { range in
            await asked.add(range)
            return StreamCacheTests.bytes(range)
        }
        let first = try await source.block(0)
        let second = try await source.block(1)
        XCTAssertEqual(first, StreamCacheTests.bytes(0...9))
        XCTAssertEqual(second, StreamCacheTests.bytes(10...19))
        let none = await asked.all
        XCTAssertEqual(none, [], "blocks already downloaded never come from the Mac")
        // Block 2 is only half written: the Mac sends it.
        let third = try await source.block(2)
        XCTAssertEqual(third, StreamCacheTests.bytes(20...29))
        let some = await asked.all
        XCTAssertEqual(some, [20...29])
        XCTAssertEqual(source.availableBytes(), [0...19])
        source.stop()
    }

    func testTheBarCountsTheDownloadedPartAndTheCachedBlocks() throws {
        let share = PartialFileShare()
        let url = try file(0...49)
        share.advance("sha", url: url, to: 15)
        let cache = StreamBlockCache(folder: folder.appendingPathComponent("cache"), limitBytes: 1000)
        cache.store("sha", 3, StreamCacheTests.bytes(30...39))
        cache.store("sha", 1, StreamCacheTests.bytes(10...19))
        let source = StreamBlockSource(key: "sha", fileSize: 50, durationMs: nil, cache: cache, blockSize: 10, local: share) { range in
            StreamCacheTests.bytes(range)
        }
        XCTAssertEqual(source.availableBytes(), [0...19, 30...39])
        XCTAssertEqual(BufferedBar.fromBytes(source.availableBytes(), fileSize: 50), [0...0.4, 0.6...0.8])
        source.stop()
    }
}

private actor AskedRanges {
    private(set) var all: [ClosedRange<Int64>] = []
    func add(_ range: ClosedRange<Int64>) { all.append(range) }
}
