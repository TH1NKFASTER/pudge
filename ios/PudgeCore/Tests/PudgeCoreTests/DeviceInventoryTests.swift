import Foundation
import XCTest
@testable import PudgeCore

/// The downloaded inventory the iPhone/iPad reports to the Mac (`swift test` in ios/PudgeCore on the Mac).
final class DeviceInventoryTests: XCTestCase {
    private let asset = "8E2B7A5C-1D4F-4C3B-9A6E-0F1E2D3C4B5A"

    func testIdsAreTheMacsOnlySortedOnceEach() {
        let inventory = DeviceInventory(isPad: false, episodes: [12, 3, 12, -2, 0], lightNovels: [-1, 7], manga: [5, 5],
                                        audiobooks: [asset, asset.lowercased(), "not-a-uuid", ""])
        XCTAssertEqual(inventory.idiom, "phone")
        XCTAssertEqual(inventory.episodes, [3, 12])
        // A previous Mac's downloads were renumbered below zero: never this Mac's items.
        XCTAssertEqual(inventory.lightNovels, [7])
        XCTAssertEqual(inventory.manga, [5])
        XCTAssertEqual(inventory.audiobooks, [asset.lowercased()])
        XCTAssertEqual(inventory.count, 5)
        XCTAssertEqual(DeviceInventory(isPad: true, episodes: [], lightNovels: [], manga: [], audiobooks: []).idiom, "pad")
    }

    func testListsStopAtTheMacsLimit() {
        let inventory = DeviceInventory(isPad: true, episodes: Array((1...(DeviceInventory.limit + 10)).reversed()),
                                        lightNovels: [], manga: [], audiobooks: [])
        XCTAssertEqual(inventory.episodes.count, DeviceInventory.limit)
        XCTAssertEqual(inventory.episodes.first, 1)
    }

    func testTheBodyHasTheKeysTheMacReads() throws {
        let inventory = DeviceInventory(isPad: true, episodes: [4], lightNovels: [2], manga: [9], audiobooks: [asset])
        let data = try JSONEncoder().encode(inventory)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["idiom", "episodes", "lightNovels", "manga", "audiobooks"])
        XCTAssertEqual(object["idiom"] as? String, "pad")
        XCTAssertEqual(object["episodes"] as? [Int], [4])
        XCTAssertEqual(object["lightNovels"] as? [Int], [2])
        XCTAssertEqual(object["manga"] as? [Int], [9])
        XCTAssertEqual(object["audiobooks"] as? [String], [asset.lowercased()])
        XCTAssertEqual(try JSONDecoder().decode(DeviceInventory.self, from: data), inventory)
        XCTAssertEqual(DeviceInventory.route, "/api/v2/inventory")
    }

    func testAChangeIsSentAndTheSameInventoryOnlyAfterTheRefreshInterval() {
        var policy = InventoryReportPolicy()
        let first = DeviceInventory(isPad: false, episodes: [1], lightNovels: [], manga: [], audiobooks: [])
        let second = DeviceInventory(isPad: false, episodes: [1, 2], lightNovels: [], manga: [], audiobooks: [])
        let now = 1_000_000.0
        XCTAssertTrue(policy.needsSending(first, serverId: "mac", now: now))
        policy.markSent(first, serverId: "mac", at: now)
        XCTAssertFalse(policy.needsSending(first, serverId: "mac", now: now + 60))
        XCTAssertTrue(policy.needsSending(second, serverId: "mac", now: now + 60))
        XCTAssertTrue(policy.needsSending(first, serverId: "mac", now: now + InventoryReportPolicy.refresh))
        // A clock set back does not stop the reports.
        XCTAssertTrue(policy.needsSending(first, serverId: "mac", now: now - 10))
    }

    func testAnotherMacOrPairingAgainGetsTheInventory() {
        var policy = InventoryReportPolicy()
        let inventory = DeviceInventory(isPad: true, episodes: [], lightNovels: [3], manga: [], audiobooks: [])
        policy.markSent(inventory, serverId: "mac-1", at: 10)
        XCTAssertTrue(policy.needsSending(inventory, serverId: "mac-2", now: 20))
        XCTAssertFalse(policy.needsSending(inventory, serverId: "mac-1", now: 20))
        policy.reset()
        XCTAssertNil(policy.sent)
        XCTAssertTrue(policy.needsSending(inventory, serverId: "mac-1", now: 20))
    }
}
