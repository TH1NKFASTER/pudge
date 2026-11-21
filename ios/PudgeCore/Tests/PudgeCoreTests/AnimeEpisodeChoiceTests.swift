import Foundation
import XCTest
@testable import PudgeCore

/// The anime card's Play and the episode list's emphasis (`swift test` in ios/PudgeCore on the Mac).
final class AnimeEpisodeChoiceTests: XCTestCase {
    private func item(_ id: Int, _ number: Int, watched: Bool = false, here: Bool = false) -> EpisodeChoiceItem {
        EpisodeChoiceItem(id: id, number: number, watched: watched, onDevice: here)
    }

    func testPlayOpensTheNearestEpisodeNotTheList() {
        // Episodes here: the next to watch plays here (not the list).
        let items = [item(1, 1, watched: true, here: true), item(2, 2, here: true), item(3, 3, here: true)]
        XCTAssertEqual(AnimeCardPlay.action(items, macAvailable: true), .playHere(2))
        XCTAssertEqual(AnimeCardPlay.action(items, macAvailable: false), .playHere(2))
        // Not downloaded: it streams from the Mac, even when a later one is here.
        let later = [item(1, 1, watched: true, here: true), item(2, 2), item(3, 3, here: true)]
        XCTAssertEqual(AnimeCardPlay.action(later, macAvailable: true), .watchFromMac(2))
        XCTAssertNil(AnimeCardPlay.action(later, macAvailable: false))
    }

    func testTheCopyHereWinsAndOrderIsByNumber() {
        let items = [item(9, 4), item(5, 2), item(7, 2, here: true), item(3, 3)]
        XCTAssertEqual(AnimeCardPlay.next(items)?.id, 7)
        XCTAssertEqual(AnimeCardPlay.action(items, macAvailable: true), .playHere(7))
        XCTAssertEqual(AnimeCardPlay.action(items, macAvailable: true)?.id, 7)
    }

    func testNothingLeftToWatchHasNoPlay() {
        XCTAssertNil(AnimeCardPlay.action([], macAvailable: true))
        XCTAssertNil(AnimeCardPlay.action([item(1, 1, watched: true, here: true)], macAvailable: true))
    }

    func testRowsEmphasiseTheNextAndGreyTheWatched() {
        let rows = EpisodeRowStyle.rows([item(1, 1, watched: true), item(2, 2), item(3, 3), item(4, 4, watched: true)])
        XCTAssertEqual(rows[1], .watched)
        XCTAssertEqual(rows[2], .next)
        XCTAssertEqual(rows[3], .plain)
        XCTAssertEqual(rows[4], .watched)
        XCTAssertEqual(EpisodeRowStyle.of(watched: true, next: true), .next)
        XCTAssertEqual(EpisodeRowStyle.rows([item(1, 1, watched: true)])[1], .watched)
    }
}
