import Foundation
import XCTest
@testable import PudgeCore

/// An episode download cut by the Mac quitting (an update), a Wi-Fi drop or Pudge being killed
/// goes on from the bytes already here (`swift test --package-path ios/PudgeCore` on the Mac).
final class RangedResumeTests: XCTestCase {
    private let episode = PartialDownload(sha256: "ab", sizeBytes: 1_000)

    func testThePartialFileOfTheSameEditionIsKept() {
        XCTAssertEqual(RangedResume.resumeOffset(marker: episode, expected: episode, partialBytes: 600), 600)
        XCTAssertEqual(RangedResume.resumeOffset(marker: episode, expected: episode, partialBytes: 1_000), 1_000)
    }

    func testAnotherEditionOrAnUnknownPartialStartsOver() {
        let other = PartialDownload(sha256: "cd", sizeBytes: 1_000)
        XCTAssertEqual(RangedResume.resumeOffset(marker: other, expected: episode, partialBytes: 600), 0)
        XCTAssertEqual(RangedResume.resumeOffset(marker: nil, expected: episode, partialBytes: 600), 0)
        XCTAssertEqual(RangedResume.resumeOffset(marker: episode, expected: episode, partialBytes: nil), 0)
        XCTAssertEqual(RangedResume.resumeOffset(marker: episode, expected: episode, partialBytes: 1_001), 0)
    }

    func testAResumedRequestAsksForTheRestOfThisEditionOnly() {
        XCTAssertEqual(RangedResume.headers(offset: 0, sha256: "ab"), [:])
        XCTAssertEqual(RangedResume.headers(offset: 600, sha256: "ab"), ["Range": "bytes=600-", "If-Range": "\"ab\""])
    }

    func testTheMacsAnswerDecidesWhetherBytesAreAppended() {
        XCTAssertEqual(RangedResume.decide(status: 206, contentRange: "bytes 600-999/1000", offset: 600, total: 1_000), .append)
        // If-Range did not match (another edition): the whole file comes, the partial starts over.
        XCTAssertEqual(RangedResume.decide(status: 200, contentRange: nil, offset: 600, total: 1_000), .restart)
        XCTAssertEqual(RangedResume.decide(status: 416, contentRange: "bytes */1000", offset: 1_000, total: 1_000), .complete)
        // 206 for other bytes than asked for: never appended (a stitched file), the partial starts over.
        XCTAssertEqual(RangedResume.decide(status: 206, contentRange: "bytes 0-999/1000", offset: 600, total: 1_000), .startOver)
        XCTAssertEqual(RangedResume.decide(status: 206, contentRange: "bytes 600-999/2000", offset: 600, total: 1_000), .startOver)
        XCTAssertEqual(RangedResume.decide(status: 206, contentRange: "bytes 600-899/1000", offset: 600, total: 1_000), .startOver)
        XCTAssertEqual(RangedResume.decide(status: 206, contentRange: nil, offset: 600, total: 1_000), .startOver)
        XCTAssertTrue(RangedResume.startsOver(alreadyRestarted: false))
        XCTAssertFalse(RangedResume.startsOver(alreadyRestarted: true))
        XCTAssertEqual(RangedResume.decide(status: 400, contentRange: nil, offset: 600, total: 1_000), .refuse)
        XCTAssertEqual(RangedResume.decide(status: 416, contentRange: "bytes */1000", offset: 600, total: 1_000), .refuse)
    }

    func testContentRangeIsParsed() {
        let range = RangedResume.contentRange("bytes 100-999/1000")
        XCTAssertEqual(range?.start, 100); XCTAssertEqual(range?.end, 999); XCTAssertEqual(range?.total, 1_000)
        XCTAssertNil(RangedResume.contentRange("bytes */1000"))
        XCTAssertNil(RangedResume.contentRange("items 1-2/3"))
        XCTAssertNil(RangedResume.contentRange(nil))
    }

    func testProgressGoesOnFromTheBytesAlreadyHere() {
        XCTAssertEqual(RangedResume.fraction(received: 600, total: 1_000), 0.6, accuracy: 1e-9)
        XCTAssertEqual(RangedResume.fraction(received: 0, total: 0), 0)
        XCTAssertEqual(RangedResume.fraction(received: 2_000, total: 1_000), 1)
    }

    func testTheMarkerSitsNextToThePartialFile() {
        let partial = URL(fileURLWithPath: "/x/video/12.part")
        XCTAssertEqual(RangedResume.markerURL(for: partial).lastPathComponent, "12.part.json")
    }

    func testADownloadWaitsMinutesForAMacThatRestarts() {
        let refused = URLError(.cannotConnectToHost)  // the Mac app quit for an update
        XCTAssertFalse(TransferRetry.shouldRetry(refused, attempt: TransferRetry.attempts))
        XCTAssertTrue(TransferRetry.shouldRetry(refused, attempt: 40, stalledFor: 5 * 60))
        XCTAssertFalse(TransferRetry.shouldRetry(refused, attempt: 200, stalledFor: TransferRetry.patience + 1))
        XCTAssertTrue(TransferRetry.shouldRetry(refused, attempt: 1, stalledFor: TransferRetry.patience + 1))
        XCTAssertFalse(TransferRetry.shouldRetry(URLError(.cancelled), attempt: 1, stalledFor: 0))
        XCTAssertFalse(TransferRetry.shouldRetry(MacRefusedStub(), attempt: 1, stalledFor: 0))
    }

    func testACutEpisodeDownloadIsKeptAtLaunchForDays() {
        let now = 10_000_000.0
        let part = StoredFile(path: "store/video/12.part", bytes: 700, modified: now - 2 * 3600)
        let marker = StoredFile(path: "store/video/12.part.json", bytes: 1, modified: now - 2 * 3600)
        let refs = StorageReferences(episodes: [12])  // an older copy of the episode is listed
        XCTAssertTrue(StorageScan.launchCleanup([part, marker], refs, now: now).isEmpty)
        XCTAssertEqual(StorageScan.verdict(marker, refs, now: now).kind, .other)  // not counted as the episode
        XCTAssertEqual(StorageScan.summarize([part, marker], refs, now: now).clearableBytes, 701)  // "Clear" removes it
        let stale = [StoredFile(path: "store/video/12.part", bytes: 700, modified: now - 4 * 86_400),
                     StoredFile(path: "store/video/12.part.json", bytes: 1, modified: now - 4 * 86_400)]
        XCTAssertEqual(StorageScan.launchCleanup(stale, refs, now: now).count, 2)
        XCTAssertTrue(StorageScan.launchCleanup(stale, StorageReferences(downloading: true), now: now).isEmpty)
    }
}

private struct MacRefusedStub: Error {}
