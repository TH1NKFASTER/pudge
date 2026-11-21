import XCTest
@testable import PudgeCore

/// IOSPERF: the iPad became slow to open and to use. The anime list was fetched and applied
/// every 15 s whenever the Mac Home had any Waiting card, words without a meaning were asked
/// again on every connect, and slow steps left no trace in the diagnostics.
final class PerfPolicyTests: XCTestCase {
    private func card(_ section: String, status: String? = nil, kind: String? = nil, fraction: Double? = nil) -> MacHomeCard {
        MacHomeCard(mediaId: 9, title: "Invented", section: section, status: status, downloadFraction: fraction, downloadKind: kind)
    }

    func testOnlyARunningDownloadOnTheMacMakesTheListFrequent() {
        func interval(_ cards: [MacHomeCard]) -> Double { AnimeLiveRefresh.interval(home: MacHomeShelves(caughtUp: [], waiting: cards)) }
        // Cards that stay for days: Action required, Available to download, waiting for subtitles.
        XCTAssertEqual(interval([card("needs_action", status: "needs_action")]), AnimeLiveRefresh.idleSeconds)
        XCTAssertEqual(interval([card("download_available")]), AnimeLiveRefresh.idleSeconds)
        XCTAssertEqual(interval([card("waiting", status: "waiting_subtitles")]), AnimeLiveRefresh.idleSeconds)
        XCTAssertEqual(interval([card("waiting", status: "downloading", kind: "stalled", fraction: 0.4)]), AnimeLiveRefresh.idleSeconds)
        XCTAssertEqual(interval([card("waiting", status: "paused_download", kind: "paused", fraction: 0.4)]), AnimeLiveRefresh.idleSeconds)
        XCTAssertEqual(interval([card("download_available", kind: "waiting")]), AnimeLiveRefresh.idleSeconds)
        // A download running now.
        XCTAssertEqual(interval([card("needs_action", status: "needs_action"), card("waiting", status: "downloading", fraction: 0.2)]),
                       AnimeLiveRefresh.busySeconds)
        XCTAssertEqual(interval([card("download_available", kind: "downloading", fraction: 0.5)]), AnimeLiveRefresh.busySeconds)
        XCTAssertEqual(interval([card("waiting", status: "waiting_download", kind: "downloading", fraction: 0.1)]), AnimeLiveRefresh.busySeconds)
    }

    func testNoTickWhileWatchingReadingOrRightAfterATouch() {
        let quiet = AnimeLiveRefresh.quietAfterTouchSeconds
        XCTAssertTrue(AnimeLiveRefresh.shouldRefresh(macAvailable: true, playerOnScreen: false, readerOnScreen: false, refreshing: false, secondsSinceTouch: quiet))
        XCTAssertFalse(AnimeLiveRefresh.shouldRefresh(macAvailable: false, playerOnScreen: false, readerOnScreen: false, refreshing: false, secondsSinceTouch: 999))
        XCTAssertFalse(AnimeLiveRefresh.shouldRefresh(macAvailable: true, playerOnScreen: true, readerOnScreen: false, refreshing: false, secondsSinceTouch: 999))
        XCTAssertFalse(AnimeLiveRefresh.shouldRefresh(macAvailable: true, playerOnScreen: false, readerOnScreen: true, refreshing: false, secondsSinceTouch: 999))
        XCTAssertFalse(AnimeLiveRefresh.shouldRefresh(macAvailable: true, playerOnScreen: false, readerOnScreen: false, refreshing: true, secondsSinceTouch: 999))
        XCTAssertFalse(AnimeLiveRefresh.shouldRefresh(macAvailable: true, playerOnScreen: false, readerOnScreen: false, refreshing: false, secondsSinceTouch: quiet - 1))
    }

    func testSlowWorkIsReportedStepByStep() {
        var perf = PerfSteps("refresh", at: 100)
        perf.mark("reach", at: 100.02)
        perf.mark("catalog", at: 102.1)
        perf.mark("sync", at: 103.0)
        XCTAssertEqual(perf.report(at: 103.2), "refresh 3.2 s: catalog 2.1 s, sync 0.9 s")
        var quick = PerfSteps("anime list", at: 5)
        quick.mark("list", at: 5.4)
        XCTAssertNil(quick.report(at: 5.9))
        XCTAssertEqual(quick.report(at: 6.0), "anime list 1.0 s: list 0.4 s")
        XCTAssertEqual(LaunchPerf.line(sinceProcessStart: 2.46, sinceAppInit: 1.94, model: 0.31),
                       "launch: first screen 2.5 s after launch, 1.9 s after the app started, data 0.3 s")
        XCTAssertEqual(LaunchPerf.line(sinceProcessStart: nil, sinceAppInit: 1, model: 0),
                       "launch: first screen shown, 1.0 s after the app started, data 0.0 s")
    }

    func testWordsWithoutAMeaningAreNotAskedOnEveryConnect() throws {
        let tokens = [StudyToken(start: 0, end: 1, wordId: 11, readingIndex: 0, state: 0),
                      StudyToken(start: 1, end: 2, wordId: 12, readingIndex: 1, state: 0)]
        var words = OfflineWords()
        XCTAssertEqual(words.missing(tokens, now: 1000), [[11, 0], [12, 1]])
        // The Mac answered neither with a complete entry.
        words.markAsked([[11, 0], [12, 1]], at: 1000)
        XCTAssertEqual(words.missing(tokens, now: 1000 + 3600), [])
        // Asked again a week later (Jiten may know them by then).
        XCTAssertEqual(words.missing(tokens, now: 1000 + OfflineWords.askAgainSeconds), [[11, 0], [12, 1]])
        // Kept across a restart; a file of an older build (without the asked words) still loads.
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("offline-words.json")
        try words.save(file)
        XCTAssertEqual(OfflineWords.load(file).missing(tokens, now: 1000 + 60), [])
        try Data(#"{"entries":{"7:0":{"savedAt":5}}}"#.utf8).write(to: file)
        let old = OfflineWords.load(file)
        XCTAssertEqual(old.entries.count, 1)
        XCTAssertTrue(old.asked.isEmpty)
    }
}
