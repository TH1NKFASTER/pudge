import XCTest
@testable import PudgeCore

/// iOS review round 5: the Mac's lists read item by item, the Mac's Finished flag kept across
/// launches, and the store's marks of this Mac's books (`swift test` in ios/PudgeCore on the Mac).
final class ContractLeniencyTests: XCTestCase {
    private struct Row: Decodable, Equatable { let id: Int; let title: String }
    private struct Wrapper: Decodable { let items: LenientList<Row> }

    func testAnUnreadableItemIsLeftOutAndNamed() throws {
        let json = #"{"items":[{"id":1,"title":"一"},{"title":"二"},{"id":"3","title":"三"},{"id":4,"title":"四"},7]}"#
        let list = try JSONDecoder().decode(Wrapper.self, from: Data(json.utf8)).items
        XCTAssertEqual(list.items, [Row(id: 1, title: "一"), Row(id: 4, title: "四")])
        XCTAssertEqual(list.skipped.count, 3)
        XCTAssertEqual(list.skipped[0], "二: no \"id\"")
        XCTAssertEqual(list.skipped[1], "三: \"id\" has another type")
        XCTAssertTrue(list.skipped[2].hasPrefix("item 5: "))
    }

    func testAListThatIsNoListStillFails() {
        XCTAssertThrowsError(try JSONDecoder().decode(Wrapper.self, from: Data(#"{"items":{"id":1}}"#.utf8)))
    }

    func testTheKeptFinishedFlagCountsOnlyWhenTheMacSaidFinished() {
        var flags = MacFinishedFlags()
        XCTAssertNil(flags.finished("a"))
        XCTAssertTrue(flags.record("a", finished: true))
        XCTAssertFalse(flags.record("a", finished: true))  // nothing to save again
        XCTAssertEqual(flags.finished("a"), true)
        // A finished book listened to again, the app launched without the Mac: still finished.
        XCTAssertTrue(AudiobookFinish.isFinished(local: nil, mac: flags.finished("a"), positionMs: 1_000, durationMs: 600_000))
        // Not finished on the Mac: the place decides (played to the end offline counts).
        XCTAssertTrue(flags.record("a", finished: false))
        XCTAssertNil(flags.finished("a"))
        XCTAssertTrue(AudiobookFinish.isFinished(local: nil, mac: flags.finished("a"), positionMs: 599_000, durationMs: 600_000))
        // This device's own Mark unfinished wins.
        flags.record("b", finished: true)
        XCTAssertFalse(AudiobookFinish.isFinished(local: false, mac: flags.finished("b"), positionMs: 0, durationMs: 600_000))
        XCTAssertTrue(flags.keep(["b"]))
        XCTAssertEqual(flags.flags, ["b": true])
        XCTAssertFalse(flags.keep(["b", "c"]))
    }

    private func book(_ asset: String, title: String = "本") -> AudioManifest {
        AudioManifest(assetId: asset, editionId: UUID().uuidString.lowercased(), timelineRevision: "r1", title: title, durationMs: 1000,
                      parts: [AudioPart(partId: UUID().uuidString.lowercased(), resourceId: UUID().uuidString.lowercased(),
                                        sha256: String(repeating: "a", count: 64), sizeBytes: 10, fileName: "a.m4a",
                                        offsetMs: 0, durationMs: 1000)], chapters: [])
    }

    func testTheStoreListsThisMacsBooksAndSkipsUnreadableManifests() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("store-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        let server = UUID().uuidString.lowercased()
        let first = book(UUID().uuidString.lowercased()), second = book(UUID().uuidString.lowercased())
        try store.saveManifest(first); try store.saveManifest(second)
        try store.markRemote(first, serverId: server)
        XCTAssertEqual(try store.remoteIds(serverId: server), [first.id])
        XCTAssertEqual(try store.remoteIds(serverId: UUID().uuidString.lowercased()), [])
        XCTAssertEqual(try store.manifests().count, 2)
        XCTAssertEqual(try store.unreadableManifests(), [])
    }
}
