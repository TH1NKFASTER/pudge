import Foundation
import XCTest
@testable import PudgeCore

/// A selection in the player's subtitle line against the player's gestures (`swift test` in
/// ios/PudgeCore on the Mac).
final class SubtitleSelectionTests: XCTestCase {
    func testThePlayerKeepsEveryTouchWithoutASelection() {
        for onHandle in [false, true] {
            for onText in [false, true] {
                for dragging in [false, true] {
                    XCTAssertFalse(SubtitleSelectionTouches.playerYields(hasSelection: false, onHandle: onHandle,
                                                                         onSelectedText: onText, dragging: dragging))
                }
            }
        }
    }

    func testHandlesSelectedTextAndADragBelongToTheSelection() {
        XCTAssertTrue(SubtitleSelectionTouches.playerYields(hasSelection: true, onHandle: true, onSelectedText: false, dragging: false))
        XCTAssertTrue(SubtitleSelectionTouches.playerYields(hasSelection: true, onHandle: false, onSelectedText: true, dragging: false))
        // A second finger while a handle is dragged never starts the pinch.
        XCTAssertTrue(SubtitleSelectionTouches.playerYields(hasSelection: true, onHandle: false, onSelectedText: false, dragging: true))
        // Elsewhere on the video: the player's tap (it also drops the selection with the menu).
        XCTAssertFalse(SubtitleSelectionTouches.playerYields(hasSelection: true, onHandle: false, onSelectedText: false, dragging: false))
    }

    func testTheSelectedLineStaysWhileTheClockMovesOn() {
        // Next line, a gap between lines: the selected line stays.
        XCTAssertEqual(SubtitleCueHold.shown(current: 4, held: 3, count: 10), 3)
        XCTAssertEqual(SubtitleCueHold.shown(current: nil, held: 3, count: 10), 3)
        // No selection: the line at the clock, or none.
        XCTAssertEqual(SubtitleCueHold.shown(current: 4, held: nil, count: 10), 4)
        XCTAssertNil(SubtitleCueHold.shown(current: nil, held: nil, count: 10))
        // A held index the track no longer has never indexes past it.
        XCTAssertEqual(SubtitleCueHold.shown(current: 1, held: 12, count: 10), 1)
        XCTAssertNil(SubtitleCueHold.shown(current: 12, held: nil, count: 10))
        XCTAssertNil(SubtitleCueHold.shown(current: nil, held: 0, count: 0))
    }
}
