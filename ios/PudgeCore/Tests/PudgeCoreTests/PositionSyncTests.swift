import XCTest
import Foundation
@testable import PudgeCore

/// Watching on the iPad and continuing on the Mac: when the place goes to the Mac
/// (`swift test` in ios/PudgeCore on the Mac).
final class PositionSyncTests: XCTestCase {
    /// Plays from `startMs` at 1× with a clock sample every half second; the times it sends.
    private func play(_ schedule: inout PositionSyncSchedule, from startMs: Int64, at start: TimeInterval,
                      for seconds: Double) -> [TimeInterval] {
        var sent: [TimeInterval] = []
        var t = 0.0
        while t <= seconds {
            let now = start + t
            if schedule.observe(positionMs: startMs + Int64(t * 1000), at: now, playing: true) {
                schedule.sent(at: now); sent.append(now)
            }
            t += 0.5
        }
        return sent
    }

    func testWhilePlayingThePlaceGoesEveryPeriod() {
        var schedule = PositionSyncSchedule(period: 15)
        let sent = play(&schedule, from: 0, at: 1000, for: 61)
        XCTAssertEqual(sent, [1015, 1030, 1045, 1060])
    }

    func testPauseSendsAtOnceAndIsNotASeek() {
        var schedule = PositionSyncSchedule(period: 100)
        _ = play(&schedule, from: 0, at: 0, for: 10)
        XCTAssertTrue(schedule.event(.paused, at: 10))
        // Paused at 10 s: the clock stands still, nothing periodic goes.
        XCTAssertFalse(schedule.observe(positionMs: 10_000, at: 40, playing: false))
        // Playing again half a minute later: not taken for a seek (the clock did not jump).
        XCTAssertFalse(schedule.observe(positionMs: 10_500, at: 41, playing: true))
    }

    func testASeekSendsAtOnceButScrubbingNotOnEveryFrame() {
        var schedule = PositionSyncSchedule(period: 15, seekJump: 5, seekGap: 2)
        _ = play(&schedule, from: 0, at: 0, for: 5)
        // +85 s in half a second: a seek, sent now.
        XCTAssertTrue(schedule.observe(positionMs: 90_000, at: 5.5, playing: true))
        schedule.sent(at: 5.5)
        // Scrubbing on within two seconds: not every frame.
        XCTAssertFalse(schedule.observe(positionMs: 200_000, at: 6.0, playing: true))
        // Back by a minute later on: sent.
        XCTAssertTrue(schedule.observe(positionMs: 140_000, at: 9.0, playing: true))
    }

    func testFastPlaybackIsNotASeek() {
        var schedule = PositionSyncSchedule(period: 15)
        XCTAssertFalse(schedule.observe(positionMs: 0, at: 0, playing: true))
        // 2× for a second: two seconds on, not a seek, not a period yet.
        XCTAssertFalse(schedule.observe(positionMs: 2_000, at: 1, playing: true))
    }

    func testCloseAndBackgroundAlwaysSendAndRestartThePeriod() {
        var schedule = PositionSyncSchedule(period: 15)
        _ = play(&schedule, from: 0, at: 0, for: 20)  // sent at 15
        XCTAssertTrue(schedule.event(.background, at: 21))
        XCTAssertTrue(schedule.event(.closed, at: 21.2))
        XCTAssertEqual(schedule.lastSentAt, 21.2)
        // The next periodic send counts from the last one.
        XCTAssertFalse(schedule.observe(positionMs: 25_000, at: 30, playing: true))
        XCTAssertTrue(schedule.observe(positionMs: 36_500, at: 36.5, playing: true))
    }

    func testWatchedRangesAreTheSecondsPlayedAsParts() {
        var seconds = IndexSet(integersIn: 0..<600)
        seconds.insert(integersIn: 900..<1000)
        XCTAssertEqual(WatchedRanges.ranges(seconds), [[0, 600], [900, 1000]])
        XCTAssertEqual(WatchedRanges.ranges(IndexSet()), [])
    }

    func testTooManyPartsKeepTheLongestInOrder() {
        var seconds = IndexSet()
        for start in stride(from: 0, to: 300, by: 3) { seconds.insert(start) }  // 100 one-second parts
        seconds.insert(integersIn: 1000..<1100)
        let parts = WatchedRanges.ranges(seconds, limit: 3)
        XCTAssertEqual(parts.count, 3)
        XCTAssertEqual(parts.last, [1000, 1100])
        XCTAssertEqual(parts, parts.sorted { $0[0] < $1[0] })
    }
}
