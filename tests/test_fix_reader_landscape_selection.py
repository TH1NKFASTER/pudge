"""Light-novel reader on the iPad in landscape: a selection starts at the text under the finger.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The mapping is
pure code in PudgeCore with XCTest cases (ios/PudgeCore/Tests/PudgeCoreTests/
ReaderSelectionHitTests.swift, `swift test --package-path ios/PudgeCore` on the Mac); the UIKit
side is guarded here by reading the Swift source, like tests/test_fix_ios_text_selection.py.

1. The reader's long press (on its scroll view) began in the paragraph found from the frames of
   every registered view and then looked the view up again by paragraph number: with two views
   of one paragraph on screen (SwiftUI keeps the copy laid out before a rotation) the touch was
   mapped in the other copy, whose lines are elsewhere, and the selection showed up far below
   the finger. Now the view the touch landed on (UIKit's hit-testing) is the one used, the
   chosen span's own view maps the point (PudgeCore `TextSelectionGeometry.pick`), and handles
   of a paragraph shown twice belong to the copy the window shows.
2. A touch maps to characters with exactly the geometry last drawn (frame and CoreText origin),
   not a layout made since for another size; the line under the finger is PudgeCore's
   `TextLineGeometry.nearest`.
3. Diagnostics: each reader selection start writes a "[reader-selection]" line (point, page,
   column, paragraph, index, the hit character's box, the view's frame and size, the window, how
   the view was found, views and copies on screen).
"""

from __future__ import annotations

import re

from source_policy import source

TEXT = "ios/Pudge/UI/JapaneseText.swift"
CORE = "ios/PudgeCore/Sources/PudgeCore/TextSelection.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/ReaderSelectionHitTests.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    """Swift text with whitespace collapsed; from `start` up to the next `end` when given."""
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_the_long_press_starts_in_the_view_the_touch_landed_on() -> None:
    controller = _swift(TEXT, "final class TextSelectionController", "final class SelectionHandleView")
    receive = controller[controller.index("if gestureRecognizer === textPress {") :]
    receive = receive[: receive.index("guard range != nil else { return false }")]
    assert "pressedView = takes ? Self.textView(touch.view) : nil" in receive
    pressed = _swift(TEXT, "@objc private func textPressed(", "func beginPress(")
    assert "hit(at: point, boundary: false, under: touched)" in pressed
    assert "position(at: point" not in pressed
    hit = _swift(TEXT, "private func hit(at point: CGPoint, boundary: Bool", "private func logStart(")
    assert "touched ?? Self.textView(shown.first?.window?.hitTest(point, with: nil))" in hit
    assert "TextSelectionGeometry.pick(spans, x: Double(point.x), y: Double(point.y), under: under)" in hit
    assert "let view = shown[chosen]" in hit
    # The view is never looked up again by its paragraph number.
    assert "paragraphIndex == index" not in hit and ".nearest(" not in hit
    position = _swift(TEXT, "private func position(at point: CGPoint, boundary: Bool)", "private struct Hit")
    assert "hit(at: point, boundary: boundary)?.place" in position


def test_a_paragraph_shown_twice_uses_the_copy_on_screen() -> None:
    visible = _swift(TEXT, "private func visibleView(_ index: Int)", "private static func textView(")
    assert "shownViews().filter { $0.paragraphIndex == index }" in visible
    assert "window.hitTest(middle, with: nil)) === view" in visible
    # point(inside:) asks about handles, which comes back here: no endless hit-testing.
    assert "guard candidates.count > 1, !resolvingCopies" in visible
    assert "defer { resolvingCopies = false }" in visible


def test_touches_map_with_the_geometry_that_was_drawn() -> None:
    view = _swift(TEXT, "final class JapaneseTextView", "final class TextSelectionController")
    draw = view[view.index("override func draw(_ rect: CGRect) {") :]
    draw = draw[: draw.index("context.saveGState()")]
    assert "drawn = (paragraph.text, frame, flipY)" in draw
    geometry = _swift(TEXT, "private func hitGeometry()", "/// The line under a height")
    assert "if let drawn, drawn.text == paragraph.text { return (drawn.frame, drawn.flipY) }" in geometry
    line = _swift(TEXT, "private func hitLine(_ y: CGFloat)", "/// UTF-16 index of the character")
    assert "TextLineGeometry.nearest(boxes, flipY: Double(hit.flipY), y: Double(y))" in line
    index = _swift(TEXT, "func characterIndex(at point: CGPoint) -> Int? {", "func glyphRect(")
    assert "guard let item = hitLine(point.y)" in index
    assert "layout(width: bounds.width)" not in index
    boundary = _swift(TEXT, "func boundary(at point: CGPoint) -> Int? {", "private static func isLineBreak")
    assert "guard let item = hitLine(point.y)" in boundary
    assert "layout(width: bounds.width)" not in boundary
    assert "nearestLine" not in view


def test_a_selection_start_writes_a_diagnostics_line() -> None:
    log = _swift(TEXT, "private func logStart(", "private func endDrag()")
    assert "guard reader else { return }" in log
    assert 'AppLog.write("reader-selection", parts.joined(separator: " "))' in log
    for part in (
        '"point "',
        '"page "',
        '"column "',
        '"paragraph "',
        '"index "',
        '"glyph "',
        '"view "',
        '"size "',
        '"window "',
        '"via "',
        '"shown "',
        '"copies "',
    ):
        assert part in log, part
    assert "view.glyphRect(at: index)" in log
    controller = _swift(TEXT, "final class TextSelectionController", "final class SelectionHandleView")
    assert 'logStart("long press", at: point, view: found?.view' in controller
    assert (
        'logStart(count == 2 ? "two taps" : "three taps", at: window, view: view, index: index, how: "tapped view")'
        in controller
    )


def test_the_mapping_is_pure_code_with_xctest_cases() -> None:
    core = _swift(CORE)
    assert (
        "public static func pick(_ spans: [Span], x: Double, y: Double, under: Int? = nil) -> Int? {" in core
    )
    assert "pick(spans, x: x, y: y).map { spans[$0].paragraph }" in core
    assert "public enum TextLineGeometry {" in core
    assert "public static func nearest(_ lines: [Line], flipY: Double, y: Double) -> Int? {" in core
    tests = source(CORE_TESTS)
    for name in (
        "testPortraitAFingerOnAParagraphPicksThatView",
        "testLandscapeTheMarginsGoToTheParagraphBeside",
        "testSideBySideColumnsStayApart",
        "testAfterRotationTheCopyUnderTheFingerWins",
        "testTheLineUnderAFingerUsesTheDrawnOrigin",
    ):
        assert f"func {name}()" in tests, name
