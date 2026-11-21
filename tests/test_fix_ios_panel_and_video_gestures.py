"""iPhone/iPad: a tap outside the word card closes it; the video player's gestures; the reader's
selection in landscape.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The rules are
PudgeCore code with XCTest (PlayerGesturesTests: VideoTaps as the gesture layer reports taps,
VideoZoom.apply; TextSelectionTests: nearest paragraph in both directions, 44-point handle
targets). What only UIKit/SwiftUI wiring can do is guarded here by reading the Swift source:

1. The word card and Look up (`bottomPanel(item:)`) put a clear catcher over everything behind
   the panel while it is open: a tap there only closes the panel (no other word, no page turn,
   no video controls). The grammar chat (`bottomPanel(isPresented:)`) keeps what is behind usable.
2. The video's taps, pinch and pan are UIKit recognizers (VideoGestureLayer) instead of SwiftUI
   gestures on a clear view: `.simultaneousGesture(pan, including: .none)` there disabled the
   tap and the pinch too (a GestureMask covers the gestures already attached to the view).
3. The reader's selection finds paragraphs by window x and y among the views on screen only, and
   a probe of SwiftUI's sizing never replaces the layout that drawing and touches use.
"""

from __future__ import annotations

import re

from source_policy import source

STUDY = "ios/Pudge/UI/StudyViews.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
TEXT = "ios/Pudge/UI/JapaneseText.swift"
GESTURES = "ios/PudgeCore/Sources/PudgeCore/PlayerGestures.swift"
GESTURE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PlayerGesturesTests.swift"
SELECTION = "ios/PudgeCore/Sources/PudgeCore/TextSelection.swift"
SELECTION_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/TextSelectionTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_a_tap_outside_the_word_card_only_closes_it() -> None:
    presenter = _flat(STUDY, "private struct BottomPanelPresenter", "extension View {")
    assert "var closesOnOutsideTap = true" in presenter
    catcher = presenter[presenter.index("if closesOnOutsideTap {") : presenter.index("BottomPanel(title:")]
    # Hit-testable, over the safe areas too, and the tap does nothing but close.
    assert "Color.clear .contentShape(Rectangle()) .ignoresSafeArea() .onTapGesture { item = nil }" in catcher
    assert ".accessibilityHidden(true)" in catcher
    # Under the panel (drawn first in the same ZStack), only while an item is shown.
    shown = presenter[presenter.index("if let current = item {") :]
    assert (
        shown.index("ZStack {") < shown.index("if closesOnOutsideTap {") < shown.index("BottomPanel(title:")
    )
    # Closing this way is a close like the others: onDismiss runs (the video resumes).
    assert "if old != nil { onDismiss?() }" in presenter
    api = _flat(STUDY, "extension View {", "")
    item_api = api[
        api.index("func bottomPanel<Item: Identifiable") : api.index("func bottomPanel<Panel: View>")
    ]
    assert "closesOnOutsideTap" not in item_api  # the word card and Look up: always
    chat_api = api[api.index("func bottomPanel<Panel: View>") :]
    assert "closesOnOutsideTap: false" in chat_api  # the grammar chat: what is behind stays usable


def test_the_video_gestures_are_a_uikit_layer_over_the_picture() -> None:
    watch = source(WATCH)
    body = watch[watch.index("var body: some View {\n        ZStack {") :]
    body = body[: body.index("\n    }\n")]
    # Over the picture, under the subtitles and the controls (they take their own touches).
    assert body.index("VideoSurface(player: video.player, onFirstFrame:") < body.index("VideoGestureLayer(")
    assert body.index("VideoGestureLayer(") < body.index("subtitle(cue, index: index)")
    assert body.index("VideoGestureLayer(") < body.index("if chrome { controls }")
    assert (
        "onTap: { action in perform(action) }" in body
        and "onZoom: { gesture in zoomGesture(gesture) }" in body
    )
    # The SwiftUI composition that disabled every gesture is gone for good.
    for gone in (
        "SpatialTapGesture",
        "MagnifyGesture",
        "including: zoom.isZoomed",
        "@State private var taps",
    ):
        assert gone not in watch, gone
    layer = _flat(WATCH, "struct VideoGestureLayer: UIViewRepresentable {", "final class VideoController")
    for part in (
        "tap.numberOfTapsRequired = 1",
        "tap.numberOfTouchesRequired = 1",
        "UIPinchGestureRecognizer(target: coordinator, action: #selector(Coordinator.pinched(_:)))",
        "pan.maximumNumberOfTouches = 1",
        "let view = PlayerTouchView()",
        "recognizer.delegate = coordinator",
        "func updateUIView(_ view: PlayerTouchView, context: Context) { context.coordinator.layer = self",
        "coordinator.stopWaiting()",
        # Every tap goes to VideoTaps at once; a lone tap settles on the main run loop later
        # (not a Task that could be dropped).
        "taps.tap(x: Double(point.x), width: Double(view.bounds.width), at: now)",
        "perform(#selector(settle), with: nil, afterDelay: VideoTaps.doubleTapInterval, inModes: [.common])",
        "NSObject.cancelPreviousPerformRequests(withTarget: self, selector: #selector(settle), object: nil)",
        "taps.settle(at: ProcessInfo.processInfo.systemUptime)",
        # The pinch keeps its focus from where it began, from the view's middle.
        "focus = CGPoint(x: point.x - view.bounds.midX, y: point.y - view.bounds.midY)",
        "layer.onZoom(.pinchEnded)",
        "layer.onZoom(.panEnded)",
        # The drag only pans a zoomed picture; pinch and pan together.
        "recognizer === pan ? layer.zoomed : true",
        "(recognizer === pinch && other === pan) || (recognizer === pan && other === pinch)",
    ):
        assert part in layer, part
    # The layer's view (PlayerTouchView, which also holds the subtitle line) takes two fingers.
    touch = _flat(WATCH, "final class PlayerTouchView: UIView {", "final class VideoController")
    assert "isMultipleTouchEnabled = true" in touch
    # The app draws its own controls: no AVKit player controller or VideoPlayer.
    assert "AVPlayerViewController" not in watch and "import AVKit" not in watch
    assert "override class var layerClass: AnyClass { AVPlayerLayer.self }" in watch
    perform = _flat(WATCH, "private func perform(_ action: VideoTapAction) {", "private func rippleView(")
    for case in ("case .toggleControls:", "chrome.toggle()", "case .togglePlay:", "video.toggle()"):
        assert case in perform
    assert (
        "ripple = SeekRipple(forward: deltaMs > 0, text: VideoTaps.label(totalMs: totalMs), serial: serial)"
        in perform
    )
    zoom = _flat(
        WATCH,
        "private func zoomGesture(_ gesture: VideoZoomGesture) {",
        "/// Height of the displayed picture",
    )
    assert "zoom.apply(gesture, picture: pictureSize, view: viewSize)" in zoom
    core = source(GESTURES)
    assert "public enum VideoZoomGesture: Equatable, Sendable {" in core
    assert (
        "public mutating func apply(_ gesture: VideoZoomGesture, picture: ZoomSize, view: ZoomSize) {" in core
    )
    tests = source(GESTURE_TESTS)
    for name in ("testRecognizerReportsDriveTheZoom", "testTapsAsTheLayerReportsThem"):
        assert f"func {name}()" in tests


def test_the_readers_selection_maps_touches_in_both_directions_on_screen_only() -> None:
    controller = _flat(TEXT, "final class TextSelectionController", "final class SelectionHandleView")
    assert "private var views: [ObjectIdentifier: Visible] = [:]" in controller
    assert "views[ObjectIdentifier(view)] = Visible(view: view)" in controller
    assert "views[ObjectIdentifier(view)] = nil" in controller
    shown = _flat(TEXT, "private func shownViews() -> [JapaneseTextView] {", "func layoutChanged()")
    for part in ("!view.isHidden", "view.alpha > 0.01", "current.isHidden", "intersects(window.bounds)"):
        assert part in shown, part
    position = _flat(
        TEXT, "private func position(at point: CGPoint, boundary: Bool)", "private func endDrag()"
    )
    # Both directions, on screen only; the view itself is picked (test_fix_reader_landscape_selection.py).
    assert (
        "TextSelectionGeometry.pick(spans, x: Double(point.x), y: Double(point.y), under: under)" in position
    )
    assert "let shown = shownViews()" in position
    assert "shownViews().filter { $0.paragraphIndex == index }" in controller
    assert "editMenu.updateVisibleMenuPosition(animated: false)" in controller
    view = _flat(TEXT, "final class JapaneseTextView", "final class TextSelectionController")
    # Rotated or resized: redraw, menu follows.
    layout = view[view.index("override func layoutSubviews() {") : view.index("override func point(inside")]
    assert "if bounds.size != laidOutSize {" in layout and "selection.layoutChanged()" in layout
    # A sizing probe at another width keeps the view's own layout (and its subtitle outline).
    assert "frameCache.first(where: { abs($0.width - width) < 0.5 })" in view
    assert "if let outline = laid.outline { CTFrameDraw(outline, context) }" in view
    assert "outlineFrame" not in view
    fits = _flat(TEXT, "func sizeThatFits(_ proposal: ProposedViewSize", "extension ReaderStyle {")
    assert "screen.bounds" not in fits and "uiView.window?.bounds.width" in fits
    core = source(SELECTION)
    assert "public static func nearest(_ spans: [Span], x: Double, y: Double) -> Int? {" in core
    assert "public static func handleTarget(" in core
    tests = source(SELECTION_TESTS)
    for name in ("testNearestParagraphInTwoDirections", "testHandleTargetsAreAtLeast44Points"):
        assert f"func {name}()" in tests
