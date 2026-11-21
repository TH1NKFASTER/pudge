import Foundation
import XCTest
@testable import PudgeCore

/// Watched episodes downloaded on the iPhone/iPad are deleted like on the Mac
/// (`swift test --package-path ios/PudgeCore` on the Mac).
final class WatchedCleanupTests: XCTestCase {
    private let hour = 3600.0
    private func watched(at: Double? = 1_000_000, kept: Bool = false, position: Int64 = 1_400_000, busy: Bool = false) -> WatchedCleanupState {
        WatchedCleanupState(watchedAt: at, kept: kept, positionMs: position, durationMs: 1_440_000, busy: busy)
    }

    func testTheChoiceGivesTheDelay() {
        XCTAssertEqual(WatchedCleanupChoice.mac.delay(macHours: 24), 24 * hour)
        XCTAssertEqual(WatchedCleanupChoice.mac.delay(macHours: 0), 0)
        XCTAssertEqual(WatchedCleanupChoice.mac.delay(macHours: -5), 0)
        // An older Mac that does not send its setting: nothing is deleted.
        XCTAssertNil(WatchedCleanupChoice.mac.delay(macHours: nil))
        XCTAssertNil(WatchedCleanupChoice.mac.delay(macHours: .nan))
        XCTAssertNil(WatchedCleanupChoice.off.delay(macHours: 24))
        XCTAssertEqual(WatchedCleanupChoice.immediately.delay(macHours: 24), 0)
        XCTAssertEqual(WatchedCleanupChoice.day.delay(macHours: 1), 24 * hour)
        XCTAssertEqual(WatchedCleanupChoice.week.delay(macHours: 1), 7 * 24 * hour)
        XCTAssertEqual(WatchedCleanupChoice(stored: nil), .mac)
        XCTAssertEqual(WatchedCleanupChoice(stored: "garbage"), .mac)
        XCTAssertEqual(WatchedCleanupChoice(stored: "week"), .week)
    }

    func testAWatchedDownloadGoesAfterTheDelay() {
        let state = watched()
        XCTAssertEqual(WatchedCleanup.deleteAt(state, delay: 24 * hour), 1_000_000 + 24 * hour)
        XCTAssertFalse(WatchedCleanup.isDue(state, delay: 24 * hour, now: 1_000_000 + 23 * hour))
        XCTAssertTrue(WatchedCleanup.isDue(state, delay: 24 * hour, now: 1_000_000 + 24 * hour))
        XCTAssertTrue(WatchedCleanup.isDue(state, delay: 0, now: 1_000_000))
    }

    func testNeverWithoutAWatchOrWhenOff() {
        XCTAssertNil(WatchedCleanup.deleteAt(watched(at: nil), delay: 0))
        XCTAssertNil(WatchedCleanup.deleteAt(watched(), delay: nil))
        XCTAssertFalse(WatchedCleanup.isDue(watched(), delay: nil, now: 9_999_999_999))
        XCTAssertNil(WatchedCleanup.deleteAt(WatchedCleanupState(watchedAt: 1, kept: false, positionMs: 10, durationMs: 0), delay: 0))
    }

    func testNeverWhileWatchedAgainUnder85Percent() {
        // Opened again from the start after the watch: it stays while it is watched.
        let again = watched(position: 600_000)
        XCTAssertNil(WatchedCleanup.deleteAt(again, delay: 0))
        XCTAssertFalse(WatchedCleanup.isDue(again, delay: 0, now: 9_999_999_999))
        // 85 % exactly counts as the end.
        XCTAssertNotNil(WatchedCleanup.deleteAt(watched(position: 1_224_000), delay: 0))
        XCTAssertNil(WatchedCleanup.deleteAt(watched(position: 1_223_999), delay: 0))
    }

    func testNeverWhenKeptOrDownloadedAgainAfterTheWatch() {
        XCTAssertNil(WatchedCleanup.deleteAt(watched(kept: true), delay: 0))
        XCTAssertFalse(WatchedCleanup.isDue(watched(kept: true), delay: 0, now: 9_999_999_999))
    }

    func testNotWhilePlayingOrDownloading() {
        let open = watched(busy: true)
        // Shown as pending, deleted once the player closes.
        XCTAssertNotNil(WatchedCleanup.deleteAt(open, delay: 0))
        XCTAssertFalse(WatchedCleanup.isDue(open, delay: 0, now: 9_999_999_999))
    }

    func testAnEpisodeWatchedOnAnotherDeviceGoesAfterTheDelay() {
        // The Mac counted it (mpv, another iPhone/iPad): the copy here was reset to the start.
        let elsewhere = WatchedCleanupState(watchedAt: 1_000_000, kept: false, positionMs: 0, durationMs: 1_440_000,
                                            watchedElsewhere: true)
        XCTAssertEqual(WatchedCleanup.deleteAt(elsewhere, delay: 24 * hour), 1_000_000 + 24 * hour)
        XCTAssertTrue(WatchedCleanup.isDue(elsewhere, delay: 0, now: 1_000_000))
        // Opened here again since: it stays while it is watched, as any rewatch.
        var again = elsewhere; again.positionMs = 300_000
        XCTAssertNil(WatchedCleanup.deleteAt(again, delay: 0))
        again.positionMs = 1_400_000
        XCTAssertNotNil(WatchedCleanup.deleteAt(again, delay: 0))
        // Kept, playing or with the deletion off it stays as before.
        var kept = elsewhere; kept.kept = true
        XCTAssertNil(WatchedCleanup.deleteAt(kept, delay: 0))
        XCTAssertNil(WatchedCleanup.deleteAt(elsewhere, delay: nil))
        var open = elsewhere; open.busy = true
        XCTAssertFalse(WatchedCleanup.isDue(open, delay: 0, now: 9_999_999_999))
        // A copy at the start that was not watched anywhere is never deleted.
        XCTAssertNil(WatchedCleanup.deleteAt(WatchedCleanupState(watchedAt: 1_000_000, kept: false, positionMs: 0, durationMs: 1_440_000), delay: 0))
    }

    func testTheListSaysWhen() {
        XCTAssertEqual(WatchedCleanup.label(deleteAt: 5 * hour, now: 0), "Deleting in 5 h")
        XCTAssertEqual(WatchedCleanup.label(deleteAt: 4.2 * hour, now: 0), "Deleting in 5 h")
        XCTAssertEqual(WatchedCleanup.label(deleteAt: 40 * 60, now: 0), "Deleting in 40 min")
        XCTAssertEqual(WatchedCleanup.label(deleteAt: 7 * 24 * hour, now: 0), "Deleting in 7 d")
        XCTAssertEqual(WatchedCleanup.label(deleteAt: 30, now: 0), "Deleting soon")
        XCTAssertEqual(WatchedCleanup.label(deleteAt: 0, now: 100), "Deleting soon")
    }
}
