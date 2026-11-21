"""iPhone/iPad text selection (light-novel reader, subtitles) and two pairing fixes.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The selection's
rules are pure code in PudgeCore with XCTest tests (ios/PudgeCore/Tests/PudgeCoreTests/
TextSelectionTests.swift, `swift test --package-path ios/PudgeCore` on the Mac): ranges over
paragraphs, word / sentence units, Copy with paragraph breaks, handle drags across paragraph
ends, double / triple taps without waiting, the menu's items. What only UIKit can do (handles,
loupe, gestures, the menu, auto-scroll) and the pairing retry are guarded here by reading the
Swift source, like tests/test_fix_ios_reader_guards.py; the Mac's pairing section is behaviour-
tested in the window.

1. After a long press, two taps (word) or three taps (sentence; a subtitle line: all of it) the
   selection keeps blue handles at both ends; a handle drags that end by characters with the
   system loupe, the menu hides while dragging and comes back after. A single tap still looks
   the word up (it waits 0.25 s for a second tap).
2. In the reader one selection spans paragraphs (paragraph + UTF-16 offset at both ends, kept by
   the reader, drawn by each paragraph on screen); near the reader's edges it scrolls along.
3. Pairing: the help names the Mac's section as it is now (Settings → Advanced → iPhone / iPad);
   on a first pairing the addresses are tried again after 3 s (iOS asks for Local Network access
   during the first attempt), and the error says what to check.
"""

from __future__ import annotations

import re

from i18n_support import en
from source_policy import source

TEXT = "ios/Pudge/UI/JapaneseText.swift"
READ = "ios/Pudge/UI/ReadScreen.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
CORE = "ios/PudgeCore/Sources/PudgeCore/TextSelection.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/TextSelectionTests.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"
SETTINGS = "ios/Pudge/UI/SettingsScreen.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    """Swift text with whitespace collapsed; from `start` up to the next `end` when given."""
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_the_selection_rules_are_pure_code_with_xctest_cases() -> None:
    tests = source(CORE_TESTS)
    for case in (
        "testEitherOrderGivesTheSameRangeAndEachParagraphItsPart",
        "testCopyKeepsParagraphBreaksAndSkipsIllustrations",
        "testEndsOnAParagraphEdgeMoveIntoWhatTheySelect",
        "testDraggingAHandleCrossesParagraphsAndSwapsPastTheOtherEnd",
        "testTwoTapsSelectTheWordOrARunOfOneKind",
        "testThreeTapsSelectTheSentenceInTheReaderAndTheWholeLineInSubtitles",
        "testTheMenuLooksUpTheFirstWordWithAnEntryAndStartsAtTheSelectionStart",
        "testTapsInARowCountWithoutWaiting",
        "testATouchThatContinuesTheRowIsATapNotAHandle",
        "testOnlyParagraphsWhosePartOrHandlesMovedRedraw",
    ):
        assert f"func {case}()" in tests, case
    # The single tap's lookup waits at most 0.25 s for a second tap.
    delay = re.search(r"public static let singleTapDelay = ([0-9.]+)", source(CORE))
    assert delay and float(delay.group(1)) <= 0.25


def test_handles_loupe_and_menu_follow_the_system_selection() -> None:
    view = _swift(TEXT, "final class JapaneseTextView", "struct JapaneseText: UIViewRepresentable")
    # Handles in the tint colour, 44-point targets reaching past the text's edges.
    assert "final class SelectionHandleView: UIView" in view and "tintColor.setFill()" in view
    # The 44-point target is PudgeCore's (TextSelectionGeometry.handleTarget, XCTest).
    assert "TextSelectionGeometry.handleTarget(minX: Double(frame.minX)" in view
    assert "override func point(inside point: CGPoint, with event: UIEvent?) -> Bool" in view
    # A handle drag: begins on the touch, the scroll view lets go, the menu hides, the loupe follows.
    moved = _swift(TEXT, "@objc private func handleMoved(", "/// The dragged end follows the finger")
    began = moved[moved.index("case .began:") : moved.index("case .changed:")]
    assert (
        began.index("stopScrolling()") < began.index("hideMenu()") < began.index("UITextLoupeSession.begin(")
    )
    assert "UISelectionFeedbackGenerator().selectionChanged()" in began
    ended = moved[moved.index("case .ended, .cancelled, .failed:") :]
    assert ended.index("endDrag()") < ended.index("showMenu()")
    assert "loupe?.invalidate()" in _swift(TEXT, "private func endDrag()", "private func stopScrolling()")
    follow = _swift(TEXT, "private func follow()", "private func endDrag()")
    assert "position(at: target, boundary: true)" in follow  # handles snap to characters
    assert "loupe?.move(to:" in follow and "trackingCaret: true" in follow
    # A touch on a handle takes it, unless it is the next tap of a double or triple tap (after
    # two taps a short word's handles cover it): the paragraph views and the handles' drag agree.
    paragraph = _swift(TEXT, "final class JapaneseTextView", "final class TextSelectionController")
    # (tests/test_fix_ios_player_handles_wheels_size_watched.py: with a diagnostics line.)
    assert "if selection.takesHandle(at: point) {" in paragraph
    controller = _swift(TEXT, "final class TextSelectionController", "final class SelectionHandleView")
    assert "if gestureRecognizer === handlePress || gestureRecognizer === viewHandlePress {" in controller
    assert "let takes = takesHandle(at: point)" in controller and "return takes" in controller
    assert (
        "return !taps.wouldContinue(time: CACurrentMediaTime(), x: Double(point.x), y: Double(point.y))"
        " && handle(at: point) != nil" in controller
    )
    # Without the reader the handles' drag listens on the window (SwiftUI's hit-testing stops at
    # the text view's frame; the end knob hangs below the last line).
    register = _swift(TEXT, "func register(_ view: JapaneseTextView)", "private func placeHandlePress(")
    assert "if reader { reachHost(from: view) } else { placeHandlePress(in: view.window) }" in register
    assert "if !reader { endDrag() placeHandlePress(in: nil) }" in register
    # Only paragraphs whose selected part or handles moved redraw.
    assert (
        "TextSelectionRange.changes(old, normal, paragraph: index, length: length(of: index))" in controller
    )


def test_taps_long_press_and_the_reader_share_one_selection() -> None:
    controller = _swift(TEXT, "final class TextSelectionController", "final class SelectionHandleView")
    tapped = _swift(TEXT, "func tapped(_ view: JapaneseTextView", "func beginPress(")
    # Two or three taps select at once; one tap with text selected only drops it; otherwise
    # the single tap's action runs after the delay unless a second tap came.
    assert "TextUnits.taps(count, in: view.paragraph, at: index, triple: view.tripleTap)" in tapped
    assert "if range != nil { clear(); return }" in tapped
    assert "Task.sleep(nanoseconds: UInt64(TapSequence.singleTapDelay * 1_000_000_000))" in tapped
    assert "guard let self, self.tapGeneration == waiting else { return }" in tapped
    # The action is decided when the tap lands (the next subtitle line may be up when it runs).
    assert tapped.index("let action = view.singleTapAction(at: point)") < tapped.index("Task {")
    assert "guard self.range == nil else { return } action()" in tapped
    # Only a tap drops a waiting tap: a touch that starts a scroll does not.
    began = _swift(TEXT, "func touchBegan(at point: CGPoint)", "func takesHandle(")
    assert "tapGeneration" not in began
    # The reader's long press is on its scroll view, so it goes on while its paragraph scrolls
    # away; the paragraph views leave their own off there.
    assert (
        "let hold = UILongPressGestureRecognizer(target: self, action: #selector(textPressed(_:)))"
        in controller
    )
    pressed = _swift(TEXT, "@objc private func textPressed(", "func beginPress(")
    # Mapped in the view the touch landed on (test_fix_reader_landscape_selection.py).
    assert "let found = hit(at: point, boundary: false, under: touched)" in pressed
    assert "guard let place = found?.place" in pressed
    assert "pressRecognizer?.isEnabled = controller == nil" in _swift(
        TEXT, "func use(", "override func didMoveToWindow()"
    )
    # A paragraph view made again or reused keeps the reader's selection; a subtitle line's
    # own selection goes with its text.
    assert (
        "if shared == nil, paragraph.text != oldValue.text { ownSelection?.textChanged(paragraph: 0) }"
        in _swift(TEXT, "final class JapaneseTextView", "var style = ReaderStyle()")
    )
    # In the reader the handles' drag, the menu and the loupe live in its scroll view, which
    # scrolls along near its edges.
    assert "if let scroll = current as? UIScrollView { attach(to: scroll) return }" in controller
    assert "TextSelectionAutoscroll.step(" in controller and "CADisplayLink(target: self" in controller
    assert "scroll.panGestureRecognizer.addTarget(self, action: #selector(scrolled(_:)))" in controller
    # The reader keeps one selection for the chapter and hands it to every paragraph.
    reader = _swift(READ, "struct NovelReaderScreen", "private struct MarkerLine")
    assert "@State private var textSelection = TextSelectionController(acrossParagraphs: true)" in reader
    assert (
        "selection: textSelection, paragraphIndex: index, onCurrentLine: { line in placeSpokenLine(index, line) })"
        in reader
    )
    assert "textSelection.clear() // its places are this chapter's paragraphs" in reader
    assert "textSelection.paragraph = { index in chapter?.paragraphs[safe: index] }" in reader
    assert (
        "textSelection.onListenFrom = { place in listen(paragraph: place.paragraph, utf16: place.offset) }"
        in reader
    )
    # Subtitles: the line selects on its own, three taps take all of it, selecting pauses the
    # video, and a Look Up from the selection resumes it afterwards like a tap on a word.
    watch = _swift(WATCH)
    assert (
        "onSelect: { heldCue = index; if video.playing { resumeAfterLookup = true }; video.pause() },"
        in watch
    )
    # The line with words is the touch layer's text view now (PlayerTouchView), three taps the line.
    assert "line.tripleTap = .paragraph" in watch
    assert "resumeAfterLookup = resumeAfterLookup || video.playing" in watch
    assert ".onChange(of: video.playing) { _, playing in if playing { resumeAfterLookup = false } }" in watch


def test_the_pairing_help_names_the_macs_section(page) -> None:
    result = page.run(
        r"""
setPage('settings', true); await __tick(100);
const category = document.querySelector('.settings-categories [data-settings-category="advanced"]');
await __click('.settings-categories [data-settings-category="advanced"]');
await __tick(2100);
const block = document.getElementById('iphoneNativeSettings');
return {category: category.textContent.trim(), heading: block.querySelector('h3').textContent.trim(),
        show: __text('#iphoneShow'), visible: __visible('#iphoneNativeSettings')};
""",
        before=r"""
__api.iphone_status = async () => ({enabled: true, devices: [], pending: [], paired: false, pairedName: null, expiresAt: null});
""",
    )
    value = result.value
    assert value["visible"] is True
    assert value["category"] == en("ui.advanced") and value["heading"] == en("pairing.heading")
    assert value["show"] == en("pairing.showCode")
    help_text = re.search(r'Text\("(On the Mac open Pudge[^"]*)"\)', source(SETTINGS)).group(1)
    assert (
        f"Pudge → Settings → {value['category']} → {value['heading']} and show the pairing code" in help_text
    )
    assert "Audiobooks" not in help_text


def test_pairing_tries_the_addresses_again_after_the_local_network_prompt() -> None:
    reach = _swift(MODEL, "private func reachableMac(", "func disconnect()")
    assert (
        reach.index("for attempt in 1...2 {")
        < reach.index("if attempt > 1 { try await Task.sleep(nanoseconds: 3_000_000_000)")
        < reach.index('if pairingCancelled { throw PudgeError.invalid("Pairing cancelled") } }')
        < reach.index("for address in payload.candidates {")
    )
    # A cancel left over from an earlier pairing does not stop this one.
    flow = _swift(MODEL, "private func pairWithMac(", "func cancelPairing()")
    assert flow.index("pairingCancelled = false") < flow.index("var mac = try await reachableMac(payload)")
    # Each probe connection is closed (its URLSessions held their delegates until invalidated).
    assert "let answered = await client.answers()" in reach
    assert reach.index("client.close()") < reach.index("if answered { return mac }")
    failure = reach[reach.index("throw PudgeError.invalid(") :]
    for hint in (
        "same Wi-Fi network",
        "Local Network access is on for Pudge",
        "Settings → Privacy & Security → Local Network",
        "no VPN is active on this \\(device)",
    ):
        assert hint in failure, hint
