import Foundation
import XCTest
@testable import PudgeCore

/// Lock screen / headphones / Apple Watch controls of an audiobook (`swift test` in ios/PudgeCore on the Mac).
final class NowPlayingChaptersTests: XCTestCase {
    private let chapters = [
        AudioChapter(title: "One", startMs: 0, endMs: 60_000),
        AudioChapter(title: "Two", startMs: 60_000, endMs: 150_000),
        AudioChapter(title: "Three", startMs: 150_000, endMs: 200_000),
    ]

    func testNextChapterGoesToTheNextStartAndStopsAtTheLast() {
        XCTAssertEqual(NowPlayingChapters.nextStart(positionMs: 0, chapters: chapters), 60_000)
        XCTAssertEqual(NowPlayingChapters.nextStart(positionMs: 70_000, chapters: chapters), 150_000)
        XCTAssertNil(NowPlayingChapters.nextStart(positionMs: 160_000, chapters: chapters))
        XCTAssertNil(NowPlayingChapters.nextStart(positionMs: 10, chapters: []))
        // Order of the manifest does not matter.
        XCTAssertEqual(NowPlayingChapters.nextStart(positionMs: 0, chapters: Array(chapters.reversed())), 60_000)
    }

    func testPreviousRestartsTheChapterAfterThreeSeconds() {
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 64_000, chapters: chapters), 60_000)
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 63_000, chapters: chapters), 0)
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 61_000, chapters: chapters), 0)
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 151_000, chapters: chapters), 60_000)
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 2_000, chapters: chapters), 0)
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 50_000, chapters: []), 0)
        // Before the first chapter: the book's start.
        let late = [AudioChapter(title: "A", startMs: 10_000, endMs: 20_000)]
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 5_000, chapters: late), 0)
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 11_000, chapters: late), 0)
        XCTAssertEqual(NowPlayingChapters.previousStart(positionMs: 15_000, chapters: late), 10_000)
    }

    func testNowPlayingShowsTheChapterProgress() {
        let shown = NowPlayingChapters.progress(positionMs: 90_000, durationMs: 200_000, chapters: chapters)
        XCTAssertEqual(shown, NowPlayingChapters.Progress(startMs: 60_000, elapsedMs: 30_000, durationMs: 90_000, number: 2, count: 3))
        let whole = NowPlayingChapters.progress(positionMs: 90_000, durationMs: 200_000, chapters: [])
        XCTAssertEqual(whole, NowPlayingChapters.Progress(startMs: 0, elapsedMs: 90_000, durationMs: 200_000, number: nil, count: 0))
    }

    func testAScrubIsChapterTime() {
        XCTAssertEqual(NowPlayingChapters.scrubTarget(seconds: 10, positionMs: 90_000, durationMs: 200_000, chapters: chapters), 70_000)
        // Never past the chapter's end or before its start.
        XCTAssertEqual(NowPlayingChapters.scrubTarget(seconds: 500, positionMs: 90_000, durationMs: 200_000, chapters: chapters), 150_000)
        XCTAssertEqual(NowPlayingChapters.scrubTarget(seconds: -3, positionMs: 90_000, durationMs: 200_000, chapters: chapters), 60_000)
        XCTAssertEqual(NowPlayingChapters.scrubTarget(seconds: 42, positionMs: 1_000, durationMs: 200_000, chapters: []), 42_000)
    }

    func testRatesAreTheAppsSpeeds() {
        XCTAssertEqual(NowPlayingChapters.rates, NowPlayingChapters.rates.sorted())
        for speed: Float in [0.5, 0.75, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3] { XCTAssertTrue(NowPlayingChapters.rates.contains(speed)) }
        XCTAssertEqual(NowPlayingChapters.supportedRate(1.26), 1.25)
        XCTAssertEqual(NowPlayingChapters.supportedRate(9), 3)
        XCTAssertEqual(NowPlayingChapters.supportedRate(.nan), 1)
        XCTAssertEqual(NowPlayingChapters.skipSeconds, 15)
    }
}
