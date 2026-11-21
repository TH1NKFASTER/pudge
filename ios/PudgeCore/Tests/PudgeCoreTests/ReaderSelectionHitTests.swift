import Foundation
import XCTest
@testable import PudgeCore

/// Which paragraph view and which line a touch in the light-novel reader goes to (`swift test`
/// in ios/PudgeCore on the Mac). All frames are window coordinates of an invented layout.
final class ReaderSelectionHitTests: XCTestCase {
    typealias Span = TextSelectionGeometry.Span

    /// iPad portrait (834 pt wide): the column fills the window less the margins.
    func testPortraitAFingerOnAParagraphPicksThatView() {
        let spans = [Span(paragraph: 3, minX: 20, minY: 80, maxX: 814, maxY: 300),
                     Span(paragraph: 4, minX: 20, minY: 312, maxX: 814, maxY: 700),
                     Span(paragraph: 5, minX: 20, minY: 712, maxX: 814, maxY: 1100)]
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 400, y: 500), 1)
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 400, y: 900), 2)
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 400, y: 900, under: 2), 2)
        XCTAssertNil(TextSelectionGeometry.pick([], x: 0, y: 0))
    }

    /// iPad landscape (1180 pt wide): the 780 pt column is centred; a finger in a margin goes to
    /// the paragraph beside it, never to one half a page further down.
    func testLandscapeTheMarginsGoToTheParagraphBeside() {
        let spans = [Span(paragraph: 0, minX: 200, minY: 60, maxX: 980, maxY: 400),
                     Span(paragraph: 1, minX: 200, minY: 412, maxX: 980, maxY: 760)]
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 500, y: 300), 0)
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 40, y: 300), 0)
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 1150, y: 600), 1)
        XCTAssertEqual(TextSelectionGeometry.nearest(spans, x: 40, y: 300), 0)
    }

    /// Two text columns side by side (a spread): the column under the finger, whatever the
    /// paragraph order.
    func testSideBySideColumnsStayApart() {
        let spans = [Span(paragraph: 2, minX: 40, minY: 60, maxX: 560, maxY: 760),
                     Span(paragraph: 7, minX: 620, minY: 60, maxX: 1140, maxY: 760)]
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 700, y: 100), 1)
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 100, y: 700), 0)
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 580, y: 400), 0)  // the gap: nearer the left
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 600, y: 400), 1)
    }

    /// After a rotation SwiftUI can keep the copy of a paragraph laid out before it (here at its
    /// portrait place, first in the list) next to the one on screen. The touch is mapped in the
    /// view under the finger: picking by paragraph and looking the view up again took the stale
    /// copy, and its lines are half a page away.
    func testAfterRotationTheCopyUnderTheFingerWins() {
        let stale = Span(paragraph: 9, minX: 20, minY: 80, maxX: 814, maxY: 700)
        let earlier = Span(paragraph: 8, minX: 200, minY: 60, maxX: 980, maxY: 300)
        let shown = Span(paragraph: 9, minX: 200, minY: 312, maxX: 980, maxY: 760)
        let spans = [stale, earlier, shown]
        // UIKit's hit-testing found the view on screen: it is taken.
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 500, y: 650, under: 2), 2)
        // A view under the finger always wins over an overlapping copy of an earlier paragraph.
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 500, y: 200, under: 1), 1)
        // Without hit-testing the span under the point is the one returned (an index into the
        // views, not a paragraph number to look up again).
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 900, y: 740), 2)
        // A hit view whose frame does not hold the point (a handle's target past its edge) is
        // not trusted: the nearest one is.
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 900, y: 780, under: 0), 2)
        // An index that is not in the list is ignored.
        XCTAssertEqual(TextSelectionGeometry.pick(spans, x: 900, y: 740, under: 7), 2)
    }

    /// Lines in CoreText coordinates (y up from the text's bottom edge) mapped to view
    /// coordinates with the origin the drawing used: a 300 pt tall paragraph view (portrait)
    /// and the same text in a 200 pt view (wider, landscape).
    func testTheLineUnderAFingerUsesTheDrawnOrigin() {
        typealias Line = TextLineGeometry.Line
        let portrait = [Line(originY: 260, ascent: 20, descent: 5),
                        Line(originY: 200, ascent: 20, descent: 5),
                        Line(originY: 140, ascent: 20, descent: 5),
                        Line(originY: 80, ascent: 20, descent: 5),
                        Line(originY: 20, ascent: 20, descent: 5)]
        XCTAssertEqual(TextLineGeometry.nearest(portrait, flipY: 300, y: 25), 0)
        XCTAssertEqual(TextLineGeometry.nearest(portrait, flipY: 300, y: 152), 2)
        XCTAssertEqual(TextLineGeometry.nearest(portrait, flipY: 300, y: 295), 4)
        XCTAssertEqual(TextLineGeometry.nearest(portrait, flipY: 300, y: 1000), 4)
        XCTAssertEqual(TextLineGeometry.nearest(portrait, flipY: 300, y: -50), 0)
        let landscape = [Line(originY: 160, ascent: 20, descent: 5),
                         Line(originY: 100, ascent: 20, descent: 5),
                         Line(originY: 40, ascent: 20, descent: 5)]
        XCTAssertEqual(TextLineGeometry.nearest(landscape, flipY: 200, y: 25), 0)
        XCTAssertEqual(TextLineGeometry.nearest(landscape, flipY: 200, y: 150), 2)
        // Handle room (12 pt above and below): the origin moves up by it, the lines with it.
        XCTAssertEqual(TextLineGeometry.nearest(landscape, flipY: 188, y: 85), 1)
        XCTAssertEqual(landscape[0].top(flipY: 200), 20)
        XCTAssertEqual(landscape[0].bottom(flipY: 200), 45)
        // As near to two lines: the earlier one.
        XCTAssertEqual(TextLineGeometry.nearest([Line(originY: 60, ascent: 20, descent: 0),
                                                 Line(originY: 20, ascent: 20, descent: 0)], flipY: 100, y: 50), 0)
        XCTAssertNil(TextLineGeometry.nearest([], flipY: 100, y: 10))
    }
}
