"""IOS3: iPhone/iPad player polish.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (PlayerPolishTests: PlaybackEnd, TabReselection, PlayerChromeTimer,
SubtitleHitArea, VideoTaps in one zone, EpisodeDownloadOrder). The UIKit/SwiftUI wiring is
guarded here by reading the Swift source:

1. Played to the end, the player closes as Close does (onDisappear saves and syncs).
2. The tab shown, tapped again, pops its NavigationStack to the first screen; other tabs keep theirs.
3. A double tap seeks only with both taps in one zone (left then right are two single taps).
4. The controls hide 5 s after the last interaction, not while a panel/sheet/selection/drag lasts.
5. The subtitle line takes a touch only on its laid-out text (plus a small slop).
7. Episodes downloading to the phone go nearest to the next one to watch first, one at a time.
8. The JitenMPV outline is stroked with round joins (no miter spikes at sharp glyph corners).
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
TEXT = "ios/Pudge/UI/JapaneseText.swift"
APP = "ios/Pudge/App/PudgeApp.swift"
READ = "ios/Pudge/UI/ReadScreen.swift"
LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
GESTURES = "ios/PudgeCore/Sources/PudgeCore/PlayerGestures.swift"
POLISH = "ios/PudgeCore/Sources/PudgeCore/PlayerPolish.swift"
TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PlayerPolishTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_the_player_closes_at_the_end_of_the_episode() -> None:
    controller = _flat(
        WATCH, "final class VideoController: ObservableObject {", "struct VideoPlayerScreen: View {"
    )
    assert "@Published private(set) var ended = false" in controller
    assert "AVPlayerItem.didPlayToEndTimeNotification, object: item" in controller
    assert (
        "PlaybackEnd.reached(positionMs: self.timeMs, durationMs: self.durationMs, playing: self.playing, wasPlaying: self.wasPlaying)"
        in controller
    )
    assert "watchEnd(current)" in controller and "watchEnd(item)" in controller
    assert "NotificationCenter.default.removeObserver(endObserver)" in controller
    player = _flat(WATCH, "struct VideoPlayerScreen: View {", "/// What the controls' hide timer waits on")
    ended = player[player.index(".onChange(of: video.ended) {") :]
    ended = ended[: ended.index("}", ended.index("dismiss()"))]
    assert "guard ended else { return }" in ended and "dismiss()" in ended
    # The normal close saves the place, the parts played (watched credit) and tells the Mac.
    gone = player[player.index(".onDisappear {") :]
    assert "media.saveVideoPosition(episode.id, positionMs: video.timeMs)" in gone
    assert "media.recordPlayed(episode.id, seconds: video.collectPlayed(), positionMs: video.timeMs)" in gone
    core = _flat(POLISH)
    assert "public enum PlaybackEnd {" in core and "return !playing && wasPlaying" in core
    assert "func testTheEndClosesOnlyWhenPlaybackStoppedThere()" in source(TESTS)


def test_tapping_the_shown_tab_again_goes_back_to_its_first_screen() -> None:
    app = _flat(APP)
    assert "TabView(selection: tabSelection) {" in app
    assert (
        "if TabReselection.action(current: tab, tapped: tapped) == .popToRoot { rootRequests[tapped, default: 0] += 1 }"
        in app
    )
    for name in ("anime", "novels", "manga", "audiobooks", "statistics"):
        assert f".environment(\\.tabRootRequest, rootRequests[.{name}] ?? 0)" in app
        assert f".tag(AppTab.{name})" in app
    assert "if !path.isEmpty { path = NavigationPath() }" in app
    assert source(WATCH).count(".popsToRootOnReselect($path)") == 1
    assert source(READ).count(".popsToRootOnReselect($path)") == 2
    assert source(LIBRARY).count(".popsToRootOnReselect($path)") == 1
    # The Anime tab's only view link became a route in its path (popped with the rest).
    ready = _flat(WATCH, "@ViewBuilder private var ready: some View {", "@ViewBuilder private var planning")
    assert "NavigationLink" not in ready
    assert "public static func action<Tab: Equatable>(current: Tab, tapped: Tab) -> TabReselection" in _flat(
        POLISH
    )


def test_a_double_tap_needs_both_taps_in_one_zone() -> None:
    taps = _flat(GESTURES, "public struct VideoTaps", "/// A width and height")
    assert "guard zone == pendingZone else {" in taps
    assert "if side == direction { return seek(side, at: time) }" in taps
    assert "public var waiting: Bool { pending != nil }" in taps
    layer = _flat(WATCH, "struct VideoGestureLayer: UIViewRepresentable {", "final class PlayerTouchView")
    tapped = layer[layer.index("@objc func tapped(") : layer.index("@objc func settle()")]
    start = tapped.index("layer.onTap(action)")
    after = tapped[start : tapped.index("return }", start)]
    assert "if taps.waiting {" in after and "perform(#selector(settle)" in after
    assert "func testADoubleTapSeeksOnlyWithBothTapsInOneZone()" in source(TESTS)


def test_the_controls_hide_five_seconds_after_the_last_interaction() -> None:
    player = _flat(WATCH, "struct VideoPlayerScreen: View {", '/// The "+20 s" shown')
    assert ".task(id: ChromeIdle(interactions: interactions, shown: chrome, holding: chromeHeld)) {" in player
    assert "try await Task.sleep(nanoseconds: UInt64(PlayerChromeTimer.delay * 1_000_000_000))" in player
    assert "withAnimation(.easeInOut(duration: 0.2)) { chrome = false }" in player
    assert (
        # The open wheel panel (timing or size, IOS4) holds them like the old timing panel did.
        "wheelPanel != nil || lookup != nil || textLookup != nil || heldCue != nil || scrubbing || video.streamProblem != nil"
        in player
    )
    assert "onEditingChanged: { editing in scrubbing = editing; touched() }" in player
    for action in (
        "jumpCue(-1)",
        "video.seek(video.timeMs - 10_000)",
        "video.toggle()",
        "video.seek(video.timeMs + 10_000)",
        "jumpCue(1)",
    ):
        assert f"Button {{ touched(); {action} }}" in player
    perform = player[player.index("private func perform(_ action: VideoTapAction) {") :]
    assert perform.index("touched()") < perform.index("switch action")
    assert "public static let delay: TimeInterval = 5" in _flat(POLISH)


def test_the_subtitle_line_takes_touches_only_on_its_text() -> None:
    touch = _flat(WATCH, "final class PlayerTouchView: UIView {", "final class VideoController")
    hit = touch[touch.index("override func hitTest(") : touch.index("/// The touch landed on the line")]
    inside = hit[hit.index("line.point(inside: convert(point, to: line), with: event)") :]
    assert 'if selection.hasSelection { trace(event, "touch on the selected line"); return line }' in inside
    assert "if line.textContains(convert(point, to: line)) { return line }" in inside
    assert hit.rstrip().endswith("return self }")
    text = _flat(TEXT, "final class JapaneseTextView", "final class TextSelectionController")
    boxes = text[text.index("func textBoxes() -> [CGRect] {") : text.index("func textContains(")]
    assert "CTLineGetTypographicBounds(item.line, nil, nil, nil)" in boxes
    assert "CTLineGetTrailingWhitespaceWidth(item.line)" in boxes
    assert "x: item.origin.x, y: flipY - top" in boxes
    assert "SubtitleHitArea.hits(x: Double(point.x), y: Double(point.y), boxes: boxes, slop: slop)" in text
    assert "public static let slop = 8.0" in _flat(POLISH)


def test_phone_downloads_go_nearest_to_the_next_episode_first() -> None:
    media = _flat(MEDIA, "func downloadAll(_ items: [EpisodeItem]) {", "/// Live Jiten states")
    assert "EpisodeDownloadOrder.arrange(waiting, mediaId: { $0.mediaId }, number: { $0.number }," in media
    assert "let running = idle ? [] : [videoQueue[0]]" in media
    # One at a time: the loop downloads the queue's first, then the next.
    assert "while let next = videoQueue.first {" in media and "await download(next)" in media
    assert "EpisodeDownloadOrder.nextToWatch(watched)" in media
    assert "public enum EpisodeDownloadOrder {" in _flat(POLISH)


def test_the_subtitle_outline_has_round_joins() -> None:
    view = _flat(TEXT, "final class JapaneseTextView", "final class TextSelectionController")
    draw = view[
        view.index("if let look = style.subtitleLook {", view.index("override func draw(_ rect: CGRect)")) :
    ]
    outline = draw.index("if let outline = laid.outline { CTFrameDraw(outline, context) }")
    assert draw.index("context.setLineJoin(.round)") < outline
    assert draw.index("context.setLineCap(.round)") < outline
    # Only that renderer strokes subtitle text (the plain SwiftUI line has a shadow, no outline).
    assert source(WATCH).count("strokeWidth") == 0
    assert source(TEXT).count("kCTStrokeWidthAttributeName") == 1
