import XCTest
@testable import PudgeCore

/// The batched progress request never goes over the Mac's limit (`swift test` in ios/PudgeCore on the Mac).
final class StateBatchTests: XCTestCase {
    func testASmallLibraryIsOneRequestWithItsRevision() {
        let batches = StateBatch.batches(["a", "b", "a", "c"])
        XCTAssertEqual(batches, [["a", "b", "c"]])
        XCTAssertEqual(StateBatch.since("rev", batches: batches.count), "rev")
        XCTAssertEqual(StateBatch.kept("rev2", batches: batches.count, playingHere: false), "rev2")
    }

    func testALargeLibraryIsAskedInBatchesWithinTheMacsLimit() {
        let assets = (0..<2300).map { "book-\($0)" }
        let batches = StateBatch.batches(assets)
        XCTAssertEqual(batches.count, 5)
        XCTAssertTrue(batches.allSatisfy { $0.count <= StateBatch.macLimit && !$0.isEmpty })
        XCTAssertEqual(batches.flatMap { $0 }, assets)
        // Several answers: no `since`, nothing kept for the next one.
        XCTAssertNil(StateBatch.since("rev", batches: batches.count))
        XCTAssertNil(StateBatch.kept("rev", batches: batches.count, playingHere: false))
    }

    func testABookPlayingHereIsAskedInFullNextTime() {
        XCTAssertNil(StateBatch.kept("rev", batches: 1, playingHere: true))
    }

    func testNothingToAskIsNoRequest() {
        XCTAssertTrue(StateBatch.batches([]).isEmpty)
        XCTAssertEqual(StateBatch.batches(["a", "b", "c"], size: 0), [["a"], ["b"], ["c"]])
    }
}
