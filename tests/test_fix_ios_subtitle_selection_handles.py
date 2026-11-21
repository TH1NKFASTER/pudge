"""iPhone/iPad player: the selection handles in a subtitle line can be dragged.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (SubtitleSelectionTests: which touches the player's gestures leave to
the selection, which line stays on screen while it is selected). What only UIKit/SwiftUI wiring
can do is guarded here by reading the Swift source:

1. A knob above the first line / below the last one lies outside the subtitle text view's
   frame, where SwiftUI's hit-testing never reaches the view: the line's vertical padding is now
   room inside the text view (JapaneseTextView.handleRoom), and the handles' drag sits on the
   text view as well as on the window.
2. The handles' drag comes before every other recognizer (they wait for it to fail; it only
   takes touches on a handle and begins on the touch), and the video's gesture layer does not
   take touches on the selection (handles, selected text, or while a handle is dragged).
3. The selected line stays on screen while it is selected (the clock may reach the next line or
   a gap mid-drag, which dropped the selection); playing again drops it.
"""

from __future__ import annotations

import re

from source_policy import source

TEXT = "ios/Pudge/UI/JapaneseText.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
GESTURES = "ios/PudgeCore/Sources/PudgeCore/PlayerGestures.swift"
TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/SubtitleSelectionTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_the_decisions_are_pure_code_with_xctest_cases() -> None:
    core = _flat(GESTURES)
    assert "public enum SubtitleSelectionTouches {" in core
    assert "guard hasSelection else { return false } return dragging || onHandle || onSelectedText" in core
    assert "public enum SubtitleCueHold {" in core
    assert "if let held, held >= 0, held < count { return held }" in core
    tests = source(TESTS)
    for name in (
        "testThePlayerKeepsEveryTouchWithoutASelection",
        "testHandlesSelectedTextAndADragBelongToTheSelection",
        "testTheSelectedLineStaysWhileTheClockMovesOn",
    ):
        assert f"func {name}()" in tests, name


def test_a_knob_lands_on_the_text_view_and_its_own_handle_drag() -> None:
    view = _flat(TEXT, "final class JapaneseTextView", "final class TextSelectionController")
    assert "var handleRoom: CGFloat = 0 {" in view
    assert "+ handleRoom * 2 }" in view  # the view is taller by the room above and below
    assert "private var flipY: CGFloat { bounds.height - handleRoom }" in view
    # Every CoreText <-> view conversion goes through flipY (the reader's room is 0: unchanged).
    assert "context.translateBy(x: 0, y: flipY)" in view
    # Touches: the flipY the text was drawn with, else the current one (hitGeometry).
    assert "drawn = (paragraph.text, frame, flipY)" in view
    assert "return (laid.frame, flipY)" in view
    assert "TextLineGeometry.nearest(boxes, flipY: Double(hit.flipY), y: Double(y))" in view
    assert view.count("guard let item = hitLine(point.y)") == 2
    assert "let height = flipY" in view and "y: flipY - (item.origin.y - item.descent) - height" in view
    assert "bounds.height - point.y" not in view and "translateBy(x: 0, y: bounds.height)" not in view
    controller = _flat(TEXT, "final class TextSelectionController", "final class SelectionHandleView")
    attach = controller[
        controller.index("private func attach(to view: UIView)") : controller.index("func detach()")
    ]
    single = attach[attach.index("if !reader {") : attach.index("} else {")]
    assert "placeHandlePress(in: view.window)" in single
    assert "let own = makeHandlePress() view.addGestureRecognizer(own) viewHandlePress = own" in single
    assert "if gestureRecognizer === handlePress || gestureRecognizer === viewHandlePress {" in controller
    assert "let takes = takesHandle(at: point)" in controller
    assert "viewHandlePress.view?.removeGestureRecognizer(viewHandlePress)" in controller
    # The player's line is PlayerTouchView's text view with the same room (tests/
    # test_fix_ios_player_handles_wheels_size_watched.py); other texts keep the representable.
    representable = _flat(TEXT, "struct JapaneseText: UIViewRepresentable", "extension ReaderStyle {")
    assert "view.onDeselect = onDeselect; view.handleRoom = handleRoom" in representable
    assert "uiView.handleRoom = handleRoom return CGSize(" in representable  # measured with the room


def test_the_handles_drag_wins_and_the_video_gestures_leave_the_selection_alone() -> None:
    controller = _flat(TEXT, "final class TextSelectionController", "final class SelectionHandleView")
    required = controller[controller.index("shouldBeRequiredToFailBy otherGestureRecognizer") :]
    assert (
        "guard !reader, gestureRecognizer === handlePress || gestureRecognizer === viewHandlePress else { return false }"
        in required
    )
    assert (
        "return otherGestureRecognizer !== handlePress && otherGestureRecognizer !== viewHandlePress"
        in required
    )
    assert (
        "SubtitleSelectionTouches.playerYields(hasSelection: true, onHandle: handle(at: point) != nil,"
        in controller
    )
    assert "onSelectedText: onText, dragging: drag != nil)" in controller
    assert (
        "static func activeOwnsTouch(at point: CGPoint) -> Bool { active?.ownsTouch(at: point) ?? false }"
        in controller
    )
    layer = _flat(WATCH, "struct VideoGestureLayer: UIViewRepresentable {", "final class VideoController")
    assert (
        "func gestureRecognizer(_ recognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {"
        in layer
    )
    assert "guard TextSelectionController.activeOwnsTouch(at: point) else { return true }" in layer
    assert "if let view = recognizer.view as? PlayerTouchView, view.lineOwns(touch) { return false }" in layer


def test_the_selected_line_stays_while_it_is_selected() -> None:
    watch = _flat(WATCH)
    assert "@State private var heldCue: Int?" in watch
    assert "SubtitleCueHold.shown(current: cueIndex, held: heldCue, count: track?.cues.count ?? 0)" in watch
    # A plain line in SwiftUI; a line with words in the touch layer (PlayerTouchView).
    assert (
        "if let index = shownCue, let cue = track?.cues[index], cue.tokens.isEmpty { subtitle(cue, index: index) }"
        in watch
    )
    assert "if let index = cueIndex, let cue = track?.cues[index]" not in watch
    assert (
        "guard let index = shownCue, let cue = track?.cues[safe: index], !cue.tokens.isEmpty else { return nil }"
        in watch
    )
    subtitle = _flat(WATCH, "private var subtitleLine: SubtitleLine? {", "private var controls")
    assert (
        "onSelect: { heldCue = index; if video.playing { resumeAfterLookup = true }; video.pause() }"
        in subtitle
    )
    assert "onDeselect: { if heldCue == index { heldCue = nil } }" in subtitle
    assert "handleRoom: Self.subtitlePadding" in subtitle
    # The padding is the view's handle room (the box behind it is as tall): the box looks as before.
    assert "line.handleRoom = subtitle.handleRoom" in watch
    assert "box.frame = CGRect(x: outer.minX, y: frame.minY, width: outer.width, height: height)" in watch
    plain = _flat(WATCH, "private func subtitle(_ cue: SubtitleCue, index: Int)", "private var subtitleLine")
    assert ".padding(.vertical, Self.subtitlePadding)" in plain
    assert "private static let subtitlePadding: CGFloat = 8" in watch
    assert (
        ".onChange(of: video.playing) { _, playing in if playing, heldCue != nil "
        "{ JapaneseTextView.clearActiveSelection(); heldCue = nil } }" in watch
    )
    # The controller reports a dropped selection after the event, unless a new one came.
    controller = _flat(TEXT, "private func set(_ value: TextSelectionRange?)", "func clear()")
    assert "if old != nil, normal == nil, let report = onDeselect {" in controller
    assert "guard let self, self.range == nil else { return } report()" in controller
