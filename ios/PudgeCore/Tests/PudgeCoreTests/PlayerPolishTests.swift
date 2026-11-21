import Foundation
import XCTest
@testable import PudgeCore

/// The player closing at the end, tab re-selection, the controls' hide timer, the subtitle hit
/// area, double taps in one zone and the download order (`swift test` in ios/PudgeCore on the Mac).
final class PlayerPolishTests: XCTestCase {
    func testTheEndClosesOnlyWhenPlaybackStoppedThere() {
        XCTAssertTrue(PlaybackEnd.reached(positionMs: 1_439_500, durationMs: 1_440_000, playing: false, wasPlaying: true))
        XCTAssertTrue(PlaybackEnd.reached(positionMs: 1_440_000, durationMs: 1_440_000, playing: false, wasPlaying: true))
        // Still playing, paused by hand earlier, or not at the end: stays open.
        XCTAssertFalse(PlaybackEnd.reached(positionMs: 1_439_500, durationMs: 1_440_000, playing: true, wasPlaying: true))
        XCTAssertFalse(PlaybackEnd.reached(positionMs: 1_439_500, durationMs: 1_440_000, playing: false, wasPlaying: false))
        XCTAssertFalse(PlaybackEnd.reached(positionMs: 1_400_000, durationMs: 1_440_000, playing: false, wasPlaying: true))
        XCTAssertFalse(PlaybackEnd.reached(positionMs: 0, durationMs: 0, playing: false, wasPlaying: true))
    }

    func testTheShownTabTappedAgainGoesBackToItsFirstScreen() {
        XCTAssertEqual(TabReselection.action(current: "anime", tapped: "anime"), .popToRoot)
        XCTAssertEqual(TabReselection.action(current: "anime", tapped: "manga"), .switchTab)
        XCTAssertEqual(TabReselection.action(current: 2, tapped: 0), .switchTab)
    }

    func testControlsHideFiveSecondsAfterTheLastInteraction() {
        XCTAssertEqual(PlayerChromeTimer.delay, 5)
        XCTAssertTrue(PlayerChromeTimer.hides(shown: true, idleSeconds: 5, holding: false))
        XCTAssertFalse(PlayerChromeTimer.hides(shown: true, idleSeconds: 3, holding: false))
        // A panel, a sheet, a selection or a drag keeps them.
        XCTAssertFalse(PlayerChromeTimer.hides(shown: true, idleSeconds: 9, holding: true))
        XCTAssertFalse(PlayerChromeTimer.hides(shown: false, idleSeconds: 9, holding: false))
    }

    func testTheSubtitleLineTakesTapsOnlyOnItsText() {
        // A centred line: text 300…500 of a 900-point-wide line view, two lines.
        let boxes = [TextBox(x: 300, y: 10, width: 200, height: 40), TextBox(x: 340, y: 55, width: 120, height: 40)]
        XCTAssertTrue(SubtitleHitArea.hits(x: 400, y: 30, boxes: boxes))
        XCTAssertTrue(SubtitleHitArea.hits(x: 294, y: 30, boxes: boxes))  // within the slop
        XCTAssertTrue(SubtitleHitArea.hits(x: 400, y: 90, boxes: boxes))
        XCTAssertFalse(SubtitleHitArea.hits(x: 100, y: 30, boxes: boxes))  // far left of the words
        XCTAssertFalse(SubtitleHitArea.hits(x: 800, y: 30, boxes: boxes))  // far right
        XCTAssertFalse(SubtitleHitArea.hits(x: 320, y: 90, boxes: boxes))  // beside the shorter line
        XCTAssertFalse(SubtitleHitArea.hits(x: 400, y: 30, boxes: []))
        XCTAssertFalse(SubtitleHitArea.hits(x: .nan, y: 30, boxes: boxes))
    }

    func testADoubleTapSeeksOnlyWithBothTapsInOneZone() {
        var taps = VideoTaps()
        // Left, then right: two single taps (the first toggles the controls at once, the second waits).
        XCTAssertNil(taps.tap(x: 100, width: 900, at: 1))
        XCTAssertEqual(taps.tap(x: 800, width: 900, at: 1.15), .toggleControls)
        XCTAssertTrue(taps.waiting)
        XCTAssertEqual(taps.settle(at: 1.15 + VideoTaps.doubleTapInterval), .toggleControls)
        // A side, then the middle: no pause, no seek.
        XCTAssertNil(taps.tap(x: 800, width: 900, at: 5))
        XCTAssertEqual(taps.tap(x: 450, width: 900, at: 5.1), .toggleControls)
        XCTAssertEqual(taps.settle(at: 5.4), .toggleControls)
        // Two on the right: forward; then one on the left does not seek back on its own.
        XCTAssertNil(taps.tap(x: 800, width: 900, at: 9))
        XCTAssertEqual(taps.tap(x: 810, width: 900, at: 9.2), .seek(deltaMs: 10_000, totalMs: 10_000))
        XCTAssertNil(taps.tap(x: 100, width: 900, at: 9.5))
        XCTAssertEqual(taps.settle(at: 9.8), .toggleControls)
    }

    func testDownloadsGoNearestToTheNextEpisodeToWatchFirst() {
        XCTAssertEqual(EpisodeDownloadOrder.nextToWatch([(1, true), (2, true), (3, false), (4, false)]), 3)
        XCTAssertEqual(EpisodeDownloadOrder.nextToWatch([(5, false), (3, false), (4, false)]), 3)
        XCTAssertEqual(EpisodeDownloadOrder.nextToWatch([(1, true), (2, false), (3, true), (4, false)]), 4)
        XCTAssertEqual(EpisodeDownloadOrder.nextToWatch([(1, true), (2, true)]), 3)
        XCTAssertNil(EpisodeDownloadOrder.nextToWatch([]))

        struct Item: Equatable { let anime: Int; let number: Int }
        let items = [Item(anime: 1, number: 9), Item(anime: 2, number: 2), Item(anime: 1, number: 5),
                     Item(anime: 1, number: 2), Item(anime: 1, number: 6), Item(anime: 2, number: 1)]
        let next: [Int: Int] = [1: 5]
        let order = EpisodeDownloadOrder.arrange(items, mediaId: { $0.anime }, number: { $0.number }, next: { next[$0] })
        XCTAssertEqual(order.map(\.number), [5, 6, 9, 2, 1, 2])
        XCTAssertEqual(order.map(\.anime), [1, 1, 1, 1, 2, 2])
    }

    /// SMALL1 (review intro #12): a rewatch (every queued episode behind the next to watch)
    /// downloads from its first episode on, not from the last watched one backwards.
    func testARewatchDownloadsAscending() {
        struct Item: Equatable { let anime: Int; let number: Int }
        let rewatch = [Item(anime: 1, number: 3), Item(anime: 1, number: 1), Item(anime: 1, number: 2)]
        let order = EpisodeDownloadOrder.arrange(rewatch, mediaId: { $0.anime }, number: { $0.number }, next: { _ in 13 })
        XCTAssertEqual(order.map(\.number), [1, 2, 3])  // was [3, 2, 1]
        // Mixed: ahead of the next to watch first, ascending; the ones behind it after, ascending.
        let mixed = [4, 9, 2, 5, 6].map { Item(anime: 1, number: $0) }
        let mixedOrder = EpisodeDownloadOrder.arrange(mixed, mediaId: { $0.anime }, number: { $0.number }, next: { _ in 5 })
        XCTAssertEqual(mixedOrder.map(\.number), [5, 6, 9, 2, 4])
        XCTAssertEqual(EpisodeDownloadOrder.nextToWatch((1...12).map { (number: $0, watched: true) }), 13)
    }

    /// REVIEW (intro #1): the end notice of a stream cut short mid-episode does not close the player.
    func testTheEndNoticeEndsTheEpisodeOnlyAtItsEnd() {
        XCTAssertTrue(PlaybackEnd.noticeIsEnd(positionMs: 1_440_000, durationMs: 1_440_000))
        XCTAssertTrue(PlaybackEnd.noticeIsEnd(positionMs: 1_439_200, durationMs: 1_440_000))
        XCTAssertFalse(PlaybackEnd.noticeIsEnd(positionMs: 600_000, durationMs: 1_440_000))
        XCTAssertTrue(PlaybackEnd.noticeIsEnd(positionMs: 5_000, durationMs: 0))  // duration unknown
    }
}
