import XCTest
@testable import PudgeCore

final class PlayerWheelsTests: XCTestCase {
    /// A fast flick: the picker turns on its own and reports the row only when it stops; the
    /// player redraws meanwhile with the old value, which must not turn the wheel back.
    func testAFlickIsNotTurnedBackByRedrawsWithTheOldValue() {
        let values = SubtitleDelayWheel.values
        var sync = WheelSync()
        let zero = WheelSync.row(of: 0, in: values)
        XCTAssertEqual(sync.external(row: zero), zero)  // first value: the wheel is set
        // Redraws while the wheel decelerates: the value outside is still 0, nothing is set.
        for _ in 0..<20 { XCTAssertNil(sync.external(row: zero)) }
        // It stopped at +3.7 s: reported, then the value outside follows; no row is set.
        let landed = WheelSync.row(of: 37, in: values)
        sync.picked(row: landed)
        XCTAssertEqual(WheelSync.value(atRow: landed, in: values), 37)
        XCTAssertNil(sync.external(row: WheelSync.row(of: 37, in: values)))
        // Reset (a real change outside) turns the wheel.
        XCTAssertEqual(sync.external(row: zero), zero)
        XCTAssertNil(sync.external(row: zero))
    }

    func testRowsAndValuesStayOnTheWheel() {
        let values = SubtitleDelayWheel.values
        XCTAssertEqual(WheelSync.row(of: -100, in: values), 0)
        XCTAssertEqual(WheelSync.row(of: 100, in: values), values.count - 1)
        XCTAssertEqual(WheelSync.row(of: 500, in: values), values.count - 1)  // nearest end
        XCTAssertEqual(WheelSync.row(of: -500, in: values), 0)
        XCTAssertEqual(WheelSync.value(atRow: -3, in: values), -100)
        XCTAssertEqual(WheelSync.value(atRow: 10_000, in: values), 100)
        XCTAssertNil(WheelSync.value(atRow: 0, in: []))
        XCTAssertEqual(WheelSync.row(of: 5, in: []), 0)
        let sizes = SubtitleTextSize.values
        XCTAssertEqual(WheelSync.row(of: 125, in: sizes), WheelSync.row(of: 120, in: sizes))  // the first of two as near
        XCTAssertEqual(WheelSync.row(of: 128, in: sizes), WheelSync.row(of: 130, in: sizes))
    }

    func testTextSizeSteps() {
        XCTAssertEqual(SubtitleTextSize.values.first, 70)
        XCTAssertEqual(SubtitleTextSize.values.last, 200)
        XCTAssertEqual(SubtitleTextSize.values.count, 14)
        XCTAssertTrue(SubtitleTextSize.values.contains(SubtitleTextSize.standard))
        XCTAssertEqual(SubtitleTextSize.clamp(nil), 100)
        XCTAssertEqual(SubtitleTextSize.clamp(.nan), 100)
        XCTAssertEqual(SubtitleTextSize.clamp(.infinity), 100)
        XCTAssertEqual(SubtitleTextSize.clamp(-20), 100)
        XCTAssertEqual(SubtitleTextSize.clamp(10), 70)
        XCTAssertEqual(SubtitleTextSize.clamp(500), 200)
        XCTAssertEqual(SubtitleTextSize.clamp(134), 130)
        XCTAssertEqual(SubtitleTextSize.migrated(scale: nil), 100)
        XCTAssertEqual(SubtitleTextSize.migrated(scale: 1.2), 120)
        XCTAssertEqual(SubtitleTextSize.migrated(scale: 0.5), 70)
        XCTAssertEqual(SubtitleTextSize.fontSize(base: 30, percent: 100), 30)
        XCTAssertEqual(SubtitleTextSize.fontSize(base: 30, percent: 150), 45)
        XCTAssertEqual(SubtitleTextSize.fontSize(base: 30, percent: 999), 60)
        XCTAssertEqual(SubtitleTextSize.fontSize(base: 14, percent: 70), 12)  // never below 12 points
        XCTAssertEqual(SubtitleTextSize.label(120), "120 %")
    }

    func testAnEpisodeTheMacNoLongerListsCountsWatchedByTheTitlesProgress() {
        XCTAssertTrue(MacWatchedEpisodes.watchedOnMac(number: 5, listed: false, progress: 5))
        XCTAssertTrue(MacWatchedEpisodes.watchedOnMac(number: 3, listed: false, progress: 7))
        XCTAssertFalse(MacWatchedEpisodes.watchedOnMac(number: 6, listed: false, progress: 5))  // deleted on the Mac, not watched
        XCTAssertFalse(MacWatchedEpisodes.watchedOnMac(number: 5, listed: true, progress: 9))   // the Mac still offers it
        XCTAssertFalse(MacWatchedEpisodes.watchedOnMac(number: 5, listed: false, progress: nil)) // title unknown
        XCTAssertFalse(MacWatchedEpisodes.watchedOnMac(number: 0, listed: false, progress: 4))
        XCTAssertFalse(MacWatchedEpisodes.watchedOnMac(number: 1, listed: false, progress: 0))
    }

    func testThePhonesOwnPlaceOrWatchWins() {
        func adopts(watchedHere: Bool = false, unsent: Bool = false, playing: Bool = false) -> Bool {
            MacWatchedEpisodes.adopts(number: 4, listed: false, progress: 4, watchedHere: watchedHere,
                                      positionUnsent: unsent, playing: playing)
        }
        XCTAssertTrue(adopts())
        XCTAssertFalse(adopts(watchedHere: true))  // taken already, or counted here
        XCTAssertFalse(adopts(unsent: true))       // a place of this device waits for the Mac
        XCTAssertFalse(adopts(playing: true))      // open in the player
    }
}
