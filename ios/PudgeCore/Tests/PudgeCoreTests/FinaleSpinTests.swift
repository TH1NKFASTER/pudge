import Foundation
import XCTest
@testable import PudgeCore

/// FINALE2: only the end of the anime shows the wheel; the first spin of a wheel display runs a
/// second longer (Spin again unchanged); a flick spins it without changing the draw; the sheet is
/// sized to its content. `swift test` in ios/PudgeCore on the Mac. Invented titles only.
final class FinaleSpinTests: XCTestCase {
    func testOnlyTheFinalEpisodeOffersTheWheel() throws {
        XCTAssertTrue(AnimeFinale(mediaId: 901, title: "Invented", episode: 12).offersWheel)
        // An older Mac's "caught up with an airing title" finale is not offered.
        let json = #"{"mediaId":903,"title":"Invented Airing","kind":"latest","episode":0,"prompted":false}"#
        let latest = try JSONDecoder().decode(AnimeFinale.self, from: Data(json.utf8))
        XCTAssertFalse(latest.offersWheel)
    }

    func testTheFirstSpinRunsASecondLongerAndSpinAgainKeepsItsLength() {
        XCTAssertEqual(ScoreWheel.spinDuration(spinsBefore: 0), 2.0, accuracy: 0.0001)
        XCTAssertEqual(ScoreWheel.spinDuration(spinsBefore: 1), 1.0, accuracy: 0.0001)
        XCTAssertEqual(ScoreWheel.spinDuration(spinsBefore: 5), 1.0, accuracy: 0.0001)
    }

    func testAFlickVariesTheLengthOnlySlightlyAndCapped() {
        let slow = ScoreWheel.spinDuration(spinsBefore: 1, flickSpeed: 300)
        let fast = ScoreWheel.spinDuration(spinsBefore: 1, flickSpeed: 100_000)
        XCTAssertGreaterThan(slow, 1.0)
        XCTAssertEqual(fast, 1.0 + ScoreWheel.flickExtraMaxSeconds, accuracy: 0.0001)
        XCTAssertEqual(ScoreWheel.spinDuration(spinsBefore: 1, flickSpeed: -100_000), fast, accuracy: 0.0001)
        XCTAssertEqual(ScoreWheel.spinDuration(spinsBefore: 1, flickSpeed: .nan), 1.0, accuracy: 0.0001)
        XCTAssertTrue(ScoreWheel.isFlick(distance: 80, speed: 900))
        XCTAssertFalse(ScoreWheel.isFlick(distance: 4, speed: 900))
        XCTAssertFalse(ScoreWheel.isFlick(distance: 80, speed: 40))
    }

    func testTheSheetFitsItsContent() {
        XCTAssertEqual(FinaleSheetLayout.height(content: 500), 500 + FinaleSheetLayout.chrome)
        XCTAssertEqual(FinaleSheetLayout.height(content: 0), FinaleSheetLayout.estimate)
        XCTAssertEqual(FinaleSheetLayout.height(content: 10), FinaleSheetLayout.minimum)
        XCTAssertEqual(FinaleSheetLayout.height(content: 5000), FinaleSheetLayout.maximum)
    }
}
