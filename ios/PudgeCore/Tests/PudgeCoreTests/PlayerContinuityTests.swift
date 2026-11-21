import XCTest
@testable import PudgeCore

/// Watching on the iPad while the Mac restarts, and the count right after a watch
/// (`swift test` in ios/PudgeCore on the Mac).
final class PlayerContinuityTests: XCTestCase {
    private func group(progress: Int, total: Int? = 25) -> AnimeGroup {
        AnimeGroup(mediaId: 7, title: "Show", progress: progress, total: total, episodes: [])
    }

    func testEpisodeWatchedHereRaisesTheCountOfAnOlderList() {
        // Episode 3 watched after the list was asked for (at 100): "Watched 3 of 25" at once.
        let watched = [LocalAnimeWatch(mediaId: 7, number: 3, deliveredAt: 150)]
        XCTAssertEqual(AnimeProgressOverlay.apply([group(progress: 2)], watches: watched, listRequestedAt: 100)[0].progress, 3)
        // Not sent yet (the Mac away): the same, whatever the list's age.
        let waiting = [LocalAnimeWatch(mediaId: 7, number: 3)]
        XCTAssertEqual(AnimeProgressOverlay.apply([group(progress: 2)], watches: waiting, listRequestedAt: 10_000)[0].progress, 3)
    }

    func testListAskedForAfterTheWatchReachedTheMacIsShownAsItIs() {
        // The Mac had the watch when it made this list: its answer stands (e.g. AniList refused it).
        let watched = [LocalAnimeWatch(mediaId: 7, number: 3, deliveredAt: 150)]
        XCTAssertEqual(AnimeProgressOverlay.apply([group(progress: 2)], watches: watched, listRequestedAt: 200)[0].progress, 2)
    }

    func testNeverLowersNorPassesTheTotalNorTouchesOtherTitles() {
        let watched = [LocalAnimeWatch(mediaId: 7, number: 3), LocalAnimeWatch(mediaId: 8, number: 30)]
        XCTAssertEqual(AnimeProgressOverlay.apply([group(progress: 5)], watches: watched, listRequestedAt: 0)[0].progress, 5)
        XCTAssertEqual(AnimeProgressOverlay.progress(mac: 0, total: 2, mediaId: 7, watches: [LocalAnimeWatch(mediaId: 7, number: 3)],
                                                     listRequestedAt: 0), 2)
        XCTAssertEqual(AnimeProgressOverlay.progress(mac: 1, total: nil, mediaId: 0, watches: [LocalAnimeWatch(mediaId: 0, number: 3)],
                                                     listRequestedAt: 0), 1)
        let other = AnimeGroup(mediaId: 9, title: "Other", progress: 1, total: 12, episodes: [])
        XCTAssertEqual(AnimeProgressOverlay.apply([other], watches: watched, listRequestedAt: 0), [other])
    }

    func testMacHomeCardsGetTheCountToo() {
        let home = MacHomeShelves(caughtUp: [MacHomeCard(mediaId: 7, title: "Show", progress: 2, total: 25, section: "caught_up")], waiting: [])
        let shown = AnimeProgressOverlay.apply(home, watches: [LocalAnimeWatch(mediaId: 7, number: 3)], listRequestedAt: 0)
        XCTAssertEqual(shown?.caughtUp.first?.progress, 3)
        XCTAssertNil(AnimeProgressOverlay.apply(nil, watches: [LocalAnimeWatch(mediaId: 7, number: 3)], listRequestedAt: 0))
    }

    func testAlertWaitsWhileTheVideoIsOnScreen() {
        XCTAssertFalse(PlayerContinuity.showsAlert(message: "The network connection was lost.", videoOnScreen: true))
        XCTAssertTrue(PlayerContinuity.showsAlert(message: "The network connection was lost.", videoOnScreen: false))
        XCTAssertFalse(PlayerContinuity.showsAlert(message: nil, videoOnScreen: false))
    }

    func testStreamReconnectsOnItsOwnOnlyWhenTheMacWasAway() {
        XCTAssertTrue(PlayerContinuity.reconnects(after: .unreachable))
        XCTAssertTrue(PlayerContinuity.reconnects(after: .interrupted))
        XCTAssertFalse(PlayerContinuity.reconnects(after: .revoked))
        XCTAssertFalse(PlayerContinuity.reconnects(after: .changed))
        XCTAssertFalse(PlayerContinuity.reconnects(after: nil))
        XCTAssertEqual(PlayerContinuity.reconnectDelay(attempt: 1), 3)
        XCTAssertEqual(PlayerContinuity.reconnectDelay(attempt: 100), 15)
    }
}
