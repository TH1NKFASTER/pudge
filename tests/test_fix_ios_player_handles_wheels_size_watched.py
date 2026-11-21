"""iPhone/iPad player: selection handles that drag, a timing wheel that keeps a fast flick, a
subtitle text size wheel, and a download the Mac counted as watched.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (PlayerWheelsTests: WheelSync, SubtitleTextSize, MacWatchedEpisodes).
What only UIKit/SwiftUI wiring can do is guarded here by reading the Swift source:

1. The subtitle line with words is a UIKit text view inside the player's touch layer
   (PlayerTouchView, the VideoGestureLayer's view over the whole player): it hit-tests a selection
   handle first, then the line, then the video. In SwiftUI the handles did not move (SwiftUI
   hit-tests a hosted view only in its frame; its views and gestures came between). The video's
   recognizers leave touches on the line alone; a touch on a handle writes diagnostics lines.
2. The timing wheel is a UIKit picker set from outside only when the value changed: SwiftUI's
   wheel picker was set back to the old value on redraws while it still turned after a flick.
3. A text size wheel (70-200 %), kept on this device, applied live; since IOS4 in its own panel
   with its own button (tests/test_fix_ios4_phone_screens.py), not next to the timing wheel.
4. A download the Mac no longer lists, its title's progress past it, is marked watched here and
   starts from the beginning (it was listed as "Continue ep N").
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
TEXT = "ios/Pudge/UI/JapaneseText.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
GESTURES = "ios/PudgeCore/Sources/PudgeCore/PlayerGestures.swift"
PLAYBACK = "ios/PudgeCore/Sources/PudgeCore/EpisodePlayback.swift"
TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PlayerWheelsTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_the_decisions_are_pure_code_with_xctest_cases() -> None:
    core = _flat(GESTURES)
    assert "public struct WheelSync: Equatable, Sendable {" in core
    assert "guard row != shown else { return nil } shown = row return row" in core
    assert "public enum SubtitleTextSize {" in core
    assert "Array(stride(from: 70, through: 200, by: 10))" in core
    playback = _flat(PLAYBACK)
    assert "public enum MacWatchedEpisodes {" in playback
    assert (
        "guard !listed, number > 0, let progress, progress > 0 else { return false } return number <= progress"
        in playback
    )
    tests = source(TESTS)
    for name in (
        "testAFlickIsNotTurnedBackByRedrawsWithTheOldValue",
        "testRowsAndValuesStayOnTheWheel",
        "testTextSizeSteps",
        "testAnEpisodeTheMacNoLongerListsCountsWatchedByTheTitlesProgress",
        "testThePhonesOwnPlaceOrWatchWins",
    ):
        assert f"func {name}()" in tests, name


def test_the_line_with_words_is_in_the_touch_layer_which_hit_tests_handles_first() -> None:
    watch = source(WATCH)
    body = watch[watch.index("var body: some View {\n        ZStack {") :]
    body = body[: body.index("\n    }\n")]
    flat = re.sub(r"\s+", " ", body)
    assert (
        "VideoGestureLayer(zoomed: zoom.isZoomed, subtitle: subtitleLine, subtitleBase: subtitleBase," in flat
    )
    # SwiftUI no longer draws a line with words (no JapaneseText in the player).
    assert "JapaneseText(" not in _flat(WATCH, "struct VideoPlayerScreen: View {", '/// The "+20 s" shown')
    assert "cue.tokens.isEmpty { subtitle(cue, index: index) }" in flat
    # The line ends above the Skip button and the controls, measured where SwiftUI lays them out.
    assert ".onChange(of: proxy.frame(in: .global)) { _, frame in subtitleBase = frame }" in flat
    touch = _flat(WATCH, "final class PlayerTouchView: UIView {", "final class VideoController")
    hit = touch[touch.index("override func hitTest(") : touch.index("/// The touch landed on the line")]
    assert hit.index("selection.handle(at: inWindow(point)) != nil") < hit.index(
        "line.point(inside: convert(point, to: line), with: event)"
    )
    assert hit.rstrip().endswith("return self }")
    # One text view for the player's lifetime; a gap between lines drops its selection.
    assert "let line = JapaneseTextView()" in touch
    assert "if !line.isHidden, line.selection.hasSelection { line.selection.clear() }" in touch
    assert "let height = line.height(for: width)" in touch
    layer = _flat(WATCH, "struct VideoGestureLayer: UIViewRepresentable {", "final class PlayerTouchView")
    assert "view.show(subtitle, above: subtitleBase)" in layer
    assert "if let view = recognizer.view as? PlayerTouchView, view.lineOwns(touch) { return false }" in layer
    # The lookup, the selection hold and the pause are those of the SwiftUI line before.
    line = _flat(WATCH, "private var subtitleLine: SubtitleLine? {", "/// The subtitle box's padding")
    assert "onTap: { token in lookUp(cue, token: token) }" in line
    assert "boxed: media.subtitleLook == nil" in line


def test_a_touch_on_a_handle_writes_diagnostics() -> None:
    text = source(TEXT)
    assert 'static func log(_ text: String) { AppLog.write("selection", text) }' in text
    controller = _flat(TEXT, "final class TextSelectionController", "enum SelectionTrace {")
    assert 'trace(gestureRecognizer, "touch on a handle: takes \\(takes)' in controller
    assert "SelectionTrace.recognizers(touch)" in controller
    assert 'trace(recognizer, "began: dragging the' in controller
    assert "after \\(dragMoves) moves" in controller
    assert "guard !reader else { return }" in controller  # the reader writes none


def test_the_wheels_are_set_from_outside_only_on_a_change() -> None:
    wheel = _flat(WATCH, "struct CompactWheel: UIViewRepresentable {", "/// Jiten reviews")
    assert (
        "guard let row = state.external(row: WheelSync.row(of: wheel.value, in: wheel.values)) else { return }"
        in wheel
    )
    assert "picker.selectRow(row, inComponent: 0, animated: animated)" in wheel
    assert "state.picked(row: row)" in wheel
    assert "Picker(" not in wheel and ".pickerStyle(.wheel)" not in source(WATCH)


def test_text_size_wheel_is_applied_live_and_kept_on_the_device() -> None:
    watch = _flat(WATCH)
    assert (
        "@AppStorage(SubtitleTextSize.defaultsKey) private var textPercent = SubtitleTextSize.standard"
        in watch
    )
    assert "return CGFloat(SubtitleTextSize.fontSize(base: base, percent: textPercent))" in watch
    # Its own panel (IOS4): not inside the timing panel.
    sheet = _flat(WATCH, "struct SubtitleSizeSheet: View {", "struct CompactWheel")
    assert "CompactWheel(values: SubtitleTextSize.values, value: $textPercent," in sheet
    assert 'accessibilityLabel("Subtitle text size: \\(SubtitleTextSize.label(textPercent))")' in watch
    # The earlier Larger/Smaller menu is gone; its size is the wheel's first value.
    assert 'Button("Larger")' not in watch and "subtitleScale = " not in watch
    assert "textPercent = SubtitleTextSize.migrated(scale: scale)" in watch


def test_a_download_watched_on_the_mac_drops_its_continue_state() -> None:
    media = _flat(MEDIA)
    refresh = _flat(
        MEDIA, "private func loadEpisodes(live: Bool) async {", "/// The Mac's place of a downloaded episode"
    )
    assert refresh.index("macHome = list.home") < refresh.index("adoptMacWatches()")
    adopt = _flat(MEDIA, "private func adoptMacWatches() {", "/// The Mac's subtitle timing")
    assert (
        "MacWatchedEpisodes.adopts(number: local.number, listed: listed.contains(local.id), progress: progress,"
        in adopt
    )
    assert "watchedHere: local.watchedSent != nil, positionUnsent: local.positionDirty," in adopt
    assert "playing: playingEpisodes.contains(local.id)" in adopt
    assert "local.watchedSent = true" in adopt and "local.positionMs = 0" in adopt
    assert "try? JSONEncoder().encode(local).write(to: metaURL(local.id), options: .atomic)" in adopt
    # The cards' next episode skips a copy whose watch is known (here or on the Mac).
    assert "if local.watchedSent != nil { return true }" in _flat(WATCH)
    assert "func adoptMacWatches" in media
