import Foundation
import XCTest
@testable import PudgeCore

/// Heavy screens (Statistics, Planning) open at once from the last answer kept on the device;
/// the cache stays small and never shows another Mac's data (`swift test` in ios/PudgeCore).
final class ScreenCacheTests: XCTestCase {
    private var folder: URL!

    override func setUp() {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("screen-cache-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: folder)
    }

    struct Payload: Codable, Equatable { let period: String; let rows: [String] }

    func testScreenCacheKeepsTheLastAnswerPerKeyAndMac() {
        let cache = ScreenCache(directory: folder)
        let saved = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertNil(cache.load("stats-7d", server: "mac-a", as: Payload.self))
        XCTAssertTrue(cache.save(Payload(period: "7d", rows: ["a"]), key: "stats-7d", server: "mac-a", now: saved))
        XCTAssertTrue(cache.save(Payload(period: "30d", rows: ["b"]), key: "stats-30d", server: "mac-a", now: saved))
        let loaded = cache.load("stats-7d", server: "mac-a", as: Payload.self)
        XCTAssertEqual(loaded?.value, Payload(period: "7d", rows: ["a"]))
        XCTAssertEqual(loaded?.savedAt, saved)
        XCTAssertEqual(cache.savedAt("stats-30d", server: "mac-a"), saved)
        // Another Mac's statistics are never shown (and the file goes).
        XCTAssertNil(cache.load("stats-30d", server: "mac-b", as: Payload.self))
        XCTAssertNil(cache.savedAt("stats-30d", server: "mac-a"))
    }

    func testScreenCacheDropsUnreadableAndOtherVersionFiles() throws {
        let cache = ScreenCache(directory: folder)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let name = try XCTUnwrap(ScreenCache.fileName("stats-year"))
        try Data("{broken".utf8).write(to: folder.appendingPathComponent(name))
        XCTAssertNil(cache.load("stats-year", server: nil, as: Payload.self))
        XCTAssertFalse(FileManager.default.fileExists(atPath: folder.appendingPathComponent(name).path))
        XCTAssertNil(ScreenCache.fileName("../escape"))
        XCTAssertNil(ScreenCache.fileName(""))
        let old = folder.appendingPathComponent("stats-7d.v\(ScreenCache.version - 1).json")
        try Data("{}".utf8).write(to: old)
        cache.save(Payload(period: "all", rows: []), key: "stats-all", server: nil)
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.path))
    }

    func testScreenCacheStaysWithinItsSize() {
        let cache = ScreenCache(directory: folder, maxTotalBytes: 2500, maxEntryBytes: 2000)
        let rows = [String(repeating: "x", count: 900)]
        for (index, key) in ["stats-7d", "stats-30d", "stats-year"].enumerated() {
            XCTAssertTrue(cache.save(Payload(period: key, rows: rows), key: key, server: "m"))
            let url = folder.appendingPathComponent(ScreenCache.fileName(key)!)
            try? FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: Double(index - 100))], ofItemAtPath: url.path)
        }
        cache.prune()
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        XCTAssertFalse(names.contains(ScreenCache.fileName("stats-7d")!))
        XCTAssertTrue(names.contains(ScreenCache.fileName("stats-year")!))
        // Too big to keep: nothing saved for it.
        XCTAssertFalse(cache.save(Payload(period: "all", rows: [String(repeating: "y", count: 5000)]), key: "stats-all", server: "m"))
        XCTAssertNil(cache.load("stats-all", server: "m", as: Payload.self))
    }

    func testScreenCacheEvictsOldestFirstAndKeepsTheNewOne() {
        let now = Date()
        let files: [(name: String, bytes: Int, modified: Date)] = [
            ("stats-7d.v\(ScreenCache.version).json", 100, now.addingTimeInterval(-30)),
            ("stats-30d.v\(ScreenCache.version).json", 100, now.addingTimeInterval(-20)),
            ("planning.v\(ScreenCache.version).json", 100, now.addingTimeInterval(-40)),
            ("stats-7d.v0.json", 5, now),
        ]
        XCTAssertEqual(ScreenCache.evictions(files, maxTotal: 1000), ["stats-7d.v0.json"])
        XCTAssertEqual(ScreenCache.evictions(files, maxTotal: 150, keeping: "planning.v\(ScreenCache.version).json"),
                       ["stats-7d.v0.json", "stats-7d.v\(ScreenCache.version).json", "stats-30d.v\(ScreenCache.version).json"])
    }

    func testScreenCachePrefetchAndStaleRetries() {
        let now = Date()
        XCTAssertTrue(ScreenCache.needsPrefetch(savedAt: nil, now: now))
        XCTAssertFalse(ScreenCache.needsPrefetch(savedAt: now.addingTimeInterval(-60), now: now))
        XCTAssertTrue(ScreenCache.needsPrefetch(savedAt: now.addingTimeInterval(-601), now: now))
        // A clock that jumped back: asked again.
        XCTAssertTrue(ScreenCache.needsPrefetch(savedAt: now.addingTimeInterval(3600), now: now))
        XCTAssertEqual(ScreenCache.staleRetryDelay(attempt: 0), 1.5)
        XCTAssertEqual(ScreenCache.staleRetryDelay(attempt: 2), 6)
        XCTAssertNil(ScreenCache.staleRetryDelay(attempt: 3))
    }
}
