import XCTest
@testable import PudgeCore

/// The catalog kept with its ETag: an unchanged library is a 304 and the kept answer is used
/// again (`swift test` in ios/PudgeCore on the Mac).
final class CatalogCacheTests: XCTestCase {
    private var folder: URL!
    override func setUp() {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("catalog-cache-\(UUID().uuidString)")
    }
    override func tearDown() { try? FileManager.default.removeItem(at: folder) }

    private let route = "/api/v2/catalog?lazy=1"
    private let body = Data(#"{"protocolMajor":2,"serverId":"mac-1","items":[]}"#.utf8)

    func testNothingKeptAsksInFull() throws {
        let cache = CatalogCache(directory: folder)
        XCTAssertNil(cache.etag(route: route, serverId: "mac-1"))
        let answer = try cache.answer(route: route, serverId: "mac-1", fresh: body, sentETag: nil)
        XCTAssertEqual(answer, CatalogCache.Answer(body: body, unchanged: false))
    }

    func testAKeptAnswerIsAskedWithItsTagAndReusedOn304() throws {
        let cache = CatalogCache(directory: folder)
        cache.keep(route: route, serverId: "mac-1", etag: "\"abc\"", body: body)
        XCTAssertEqual(cache.etag(route: route, serverId: "mac-1"), "\"abc\"")
        let answer = try cache.answer(route: route, serverId: "mac-1", fresh: nil, sentETag: "\"abc\"")
        XCTAssertEqual(answer, CatalogCache.Answer(body: body, unchanged: true))
        // A changed library: the new body, not the kept one.
        let newer = Data(#"{"protocolMajor":2,"serverId":"mac-1","items":[1]}"#.utf8)
        XCTAssertEqual(try cache.answer(route: route, serverId: "mac-1", fresh: newer, sentETag: "\"abc\"").body, newer)
    }

    func testAnotherMacOrRouteSendsNoTag() {
        let cache = CatalogCache(directory: folder)
        cache.keep(route: route, serverId: "mac-1", etag: "\"abc\"", body: body)
        XCTAssertNil(cache.etag(route: route, serverId: "mac-2"))
        XCTAssertNil(cache.etag(route: "/api/v2/catalog", serverId: "mac-1"))
        XCTAssertNil(cache.etag(route: route, serverId: nil))
    }

    func testAnOlderMacWithoutETagKeepsNothing() {
        let cache = CatalogCache(directory: folder)
        cache.keep(route: route, serverId: "mac-1", etag: "\"abc\"", body: body)
        cache.keep(route: route, serverId: "mac-1", etag: nil, body: body)
        XCTAssertNil(cache.entry())
        XCTAssertNil(cache.etag(route: route, serverId: "mac-1"))
    }

    func testA304WithoutTheKeptBodyIsAnError() {
        let cache = CatalogCache(directory: folder)
        cache.keep(route: route, serverId: "mac-1", etag: "\"abc\"", body: body)
        try? FileManager.default.removeItem(at: folder.appendingPathComponent("catalog-cache.json"))
        XCTAssertNil(cache.entry())
        XCTAssertThrowsError(try cache.answer(route: route, serverId: "mac-1", fresh: nil, sentETag: "\"abc\""))
        // A 304 to a tag other than the kept one is not answered from the cache either.
        cache.keep(route: route, serverId: "mac-1", etag: "\"abc\"", body: body)
        XCTAssertThrowsError(try cache.answer(route: route, serverId: "mac-1", fresh: nil, sentETag: "\"other\""))
    }

    func testClearForgetsTheKeptAnswer() {
        let cache = CatalogCache(directory: folder)
        cache.keep(route: route, serverId: "mac-1", etag: "\"abc\"", body: body)
        cache.clear()
        XCTAssertNil(cache.entry())
    }

    func testTheStoreListsSavedManifestIdsWithoutDecodingThem() throws {
        let store = try LocalStore.open(at: folder.appendingPathComponent("store"))
        XCTAssertEqual(try store.manifestIds(), [])
        let json = """
        {"protocolMajor":2,"assetId":"00000000-0000-0000-0000-000000000001","editionId":"00000000-0000-0000-0000-000000000002","timelineRevision":"one","title":"Book","durationMs":10000,"parts":[{"partId":"00000000-0000-0000-0000-000000000003","resourceId":"00000000-0000-0000-0000-000000000004","sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","sizeBytes":1,"fileName":"one.mp3","offsetMs":0,"durationMs":10000}],"chapters":[]}
        """
        let book = try AudioManifest.decode(Data(json.utf8))
        try store.saveManifest(book)
        XCTAssertEqual(try store.manifestIds(), [book.id])
    }
}
