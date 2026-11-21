import Foundation
import XCTest
@testable import PudgeCore

/// The video player's taps, zoom and subtitle timing wheel (`swift test` in ios/PudgeCore on the Mac).
final class PlayerGesturesTests: XCTestCase {
    func testThirdsOfTheVideo() {
        XCTAssertEqual(VideoTapZone.at(x: 10, width: 900), .back)
        XCTAssertEqual(VideoTapZone.at(x: 299, width: 900), .back)
        XCTAssertEqual(VideoTapZone.at(x: 450, width: 900), .middle)
        XCTAssertEqual(VideoTapZone.at(x: 601, width: 900), .forward)
        XCTAssertEqual(VideoTapZone.at(x: 899, width: 900), .forward)
        XCTAssertEqual(VideoTapZone.at(x: 10, width: 0), .middle)
        XCTAssertEqual(VideoTapZone.at(x: .nan, width: 900), .middle)
    }

    func testALoneTapTogglesTheControlsAfterTheWait() {
        var taps = VideoTaps()
        XCTAssertNil(taps.tap(x: 450, width: 900, at: 10))
        XCTAssertNil(taps.settle(at: 10.1))  // too early
        XCTAssertEqual(taps.settle(at: 10 + VideoTaps.doubleTapInterval), .toggleControls)
        XCTAssertNil(taps.settle(at: 11))  // once
        // Taps far apart are lone taps each, also on the sides.
        XCTAssertNil(taps.tap(x: 800, width: 900, at: 20))
        XCTAssertEqual(taps.settle(at: 20.3), .toggleControls)
        XCTAssertNil(taps.tap(x: 800, width: 900, at: 21))
        XCTAssertEqual(taps.settle(at: 21.3), .toggleControls)
    }

    func testDoubleTapsSeekOrPauseAndNeverToggleTheControls() {
        var taps = VideoTaps()
        XCTAssertNil(taps.tap(x: 800, width: 900, at: 1))
        XCTAssertEqual(taps.tap(x: 805, width: 900, at: 1.2), .seek(deltaMs: 10_000, totalMs: 10_000))
        XCTAssertNil(taps.settle(at: 1.3))
        XCTAssertNil(taps.tap(x: 100, width: 900, at: 5))
        XCTAssertEqual(taps.tap(x: 100, width: 900, at: 5.25), .seek(deltaMs: -10_000, totalMs: -10_000))
        XCTAssertNil(taps.tap(x: 450, width: 900, at: 9))
        XCTAssertEqual(taps.tap(x: 455, width: 900, at: 9.2), .togglePlay)
        XCTAssertNil(taps.settle(at: 9.3))
        // Two taps too far apart: two lone taps.
        XCTAssertNil(taps.tap(x: 800, width: 900, at: 12))
        XCTAssertNil(taps.tap(x: 800, width: 900, at: 12.5))
        XCTAssertEqual(taps.settle(at: 12.8), .toggleControls)
    }

    func testFurtherTapsAddTenSecondsEach() {
        var taps = VideoTaps()
        _ = taps.tap(x: 800, width: 900, at: 1)
        XCTAssertEqual(taps.tap(x: 800, width: 900, at: 1.2), .seek(deltaMs: 10_000, totalMs: 10_000))
        XCTAssertEqual(taps.tap(x: 800, width: 900, at: 1.6), .seek(deltaMs: 10_000, totalMs: 20_000))
        XCTAssertEqual(taps.tap(x: 800, width: 900, at: 2.1), .seek(deltaMs: 10_000, totalMs: 30_000))
        XCTAssertEqual(taps.series(at: 2.2), 30_000)
        XCTAssertEqual(VideoTaps.label(totalMs: 30_000), "+30 s")
        // The other side ends the series: one tap there is a first tap, two seek back.
        XCTAssertNil(taps.tap(x: 50, width: 900, at: 2.4))
        XCTAssertEqual(taps.tap(x: 50, width: 900, at: 2.6), .seek(deltaMs: -10_000, totalMs: -10_000))
        XCTAssertEqual(taps.tap(x: 50, width: 900, at: 2.7), .seek(deltaMs: -10_000, totalMs: -20_000))
        XCTAssertEqual(VideoTaps.label(totalMs: -20_000), "−20 s")
        XCTAssertNil(taps.settle(at: 3.0))
        // After the ripple is gone a tap is a first tap again.
        XCTAssertNil(taps.series(at: 2.7 + VideoTaps.seriesWindow + 0.1))
        XCTAssertNil(taps.tap(x: 50, width: 900, at: 4))
        XCTAssertEqual(taps.settle(at: 4.3), .toggleControls)
    }

    func testAMiddleTapEndsASeries() {
        var taps = VideoTaps()
        _ = taps.tap(x: 800, width: 900, at: 1)
        _ = taps.tap(x: 800, width: 900, at: 1.2)
        XCTAssertNil(taps.tap(x: 450, width: 900, at: 1.5))
        XCTAssertNil(taps.series(at: 1.5))
        XCTAssertEqual(taps.tap(x: 450, width: 900, at: 1.7), .togglePlay)
    }

    func testPinchZoomsAroundTheFingersAndSnapsBackBelowFit() {
        let view = ZoomSize(width: 1000, height: 500)
        let picture = ZoomSize(width: 1000, height: 500)
        var zoom = VideoZoom()
        XCTAssertFalse(zoom.isZoomed)
        zoom.pinch(2, focusX: 0, focusY: 0, picture: picture, view: view)
        XCTAssertEqual(zoom.scale, 2)
        XCTAssertEqual(zoom.offsetX, 0)
        zoom.endPinch(picture: picture, view: view)
        XCTAssertTrue(zoom.isZoomed)
        // A second pinch continues from 2× around the right side.
        zoom.pinch(1.5, focusX: 250, focusY: 0, picture: picture, view: view)
        XCTAssertEqual(zoom.scale, 3, accuracy: 0.0001)
        XCTAssertEqual(zoom.offsetX, -125, accuracy: 0.0001)  // 250 − 250 × 1.5
        zoom.endPinch(picture: picture, view: view)
        // Never more than 4×; pinching back below fit follows a little, then snaps to fit.
        zoom.pinch(10, focusX: 0, focusY: 0, picture: picture, view: view)
        XCTAssertEqual(zoom.scale, VideoZoom.maxScale)
        zoom.endPinch(picture: picture, view: view)
        zoom.pinch(0.1, focusX: 0, focusY: 0, picture: picture, view: view)
        XCTAssertEqual(zoom.scale, VideoZoom.minPinchScale)
        zoom.endPinch(picture: picture, view: view)
        XCTAssertEqual(zoom, VideoZoom())
        zoom.pinch(1.03, focusX: 100, focusY: 0, picture: picture, view: view)
        zoom.endPinch(picture: picture, view: view)
        XCTAssertEqual(zoom, VideoZoom())  // about 1×: fit
    }

    func testPanStaysInsideThePicture() {
        let view = ZoomSize(width: 1000, height: 500)
        let picture = ZoomSize(width: 1000, height: 500)
        var zoom = VideoZoom()
        zoom.pan(dx: 300, dy: 0, picture: picture, view: view)
        XCTAssertEqual(zoom.offsetX, 0)  // not zoomed: no pan
        zoom.pinch(2, focusX: 0, focusY: 0, picture: picture, view: view)
        zoom.endPinch(picture: picture, view: view)
        zoom.pan(dx: 300, dy: -100, picture: picture, view: view)
        XCTAssertEqual(zoom.offsetX, 300)
        XCTAssertEqual(zoom.offsetY, -100)
        zoom.pan(dx: 900, dy: -900, picture: picture, view: view)
        XCTAssertEqual(zoom.offsetX, 500)  // (1000 × 2 − 1000) / 2
        XCTAssertEqual(zoom.offsetY, -250)
        zoom.endPan()
        zoom.pan(dx: -100, dy: 0, picture: picture, view: view)
        XCTAssertEqual(zoom.offsetX, 400)  // from where the last drag ended
        // A 4:3 picture in a 16:9 view: it moves sideways only once wider than the view.
        let narrow = ZoomSize(width: 667, height: 500)
        XCTAssertEqual(VideoZoom.limit(scale: 1.2, picture: narrow, view: view).x, 0)
        XCTAssertEqual(VideoZoom.limit(scale: 2, picture: narrow, view: view).x, 167, accuracy: 0.0001)
        XCTAssertEqual(VideoZoom.limit(scale: 2, picture: narrow, view: view).y, 250, accuracy: 0.0001)
    }

    /// The UIKit layer's reports (pinch with its focus, one-finger pan, ends) drive the zoom as the
    /// direct calls do; a pan while the pinch goes on, or at 1×, moves nothing.
    func testRecognizerReportsDriveTheZoom() {
        let view = ZoomSize(width: 1000, height: 500)
        let picture = ZoomSize(width: 1000, height: 500)
        var zoom = VideoZoom()
        zoom.apply(.pan(dx: 200, dy: 0), picture: picture, view: view)
        zoom.apply(.panEnded, picture: picture, view: view)
        XCTAssertEqual(zoom, VideoZoom())
        zoom.apply(.pinch(magnification: 1.5, focusX: 0, focusY: 0), picture: picture, view: view)
        zoom.apply(.pinch(magnification: 2, focusX: 0, focusY: 0), picture: picture, view: view)
        zoom.apply(.pan(dx: 300, dy: 0), picture: picture, view: view)  // during the pinch
        XCTAssertEqual(zoom.scale, 2)
        XCTAssertEqual(zoom.offsetX, 0)
        zoom.apply(.pinchEnded, picture: picture, view: view)
        zoom.apply(.panEnded, picture: picture, view: view)
        XCTAssertTrue(zoom.isZoomed)
        zoom.apply(.pan(dx: 120, dy: -40), picture: picture, view: view)
        XCTAssertEqual(zoom.offsetX, 120)
        XCTAssertEqual(zoom.offsetY, -40)
        zoom.apply(.panEnded, picture: picture, view: view)
        zoom.apply(.pinch(magnification: 0.4, focusX: 0, focusY: 0), picture: picture, view: view)
        zoom.apply(.pinchEnded, picture: picture, view: view)
        XCTAssertEqual(zoom, VideoZoom())  // pinched back below fit: fit
    }

    /// The layer reports every tap at once and settles `doubleTapInterval` later: a lone tap
    /// toggles the controls exactly once, a double tap never does.
    func testTapsAsTheLayerReportsThem() {
        var taps = VideoTaps()
        let wait = VideoTaps.doubleTapInterval
        XCTAssertNil(taps.tap(x: 450, width: 900, at: 100))
        XCTAssertEqual(taps.settle(at: 100 + wait), .toggleControls)
        XCTAssertNil(taps.tap(x: 450, width: 900, at: 101))
        XCTAssertNil(taps.settle(at: 101 + wait - 0.1))
        XCTAssertEqual(taps.settle(at: 101 + wait + 0.05), .toggleControls)
        // Double tap on the left: −10 s, and the waits of both taps settle to nothing.
        XCTAssertNil(taps.tap(x: 100, width: 900, at: 110))
        XCTAssertEqual(taps.tap(x: 100, width: 900, at: 110.15), .seek(deltaMs: -10_000, totalMs: -10_000))
        XCTAssertNil(taps.settle(at: 110 + wait))
        XCTAssertNil(taps.settle(at: 110.15 + wait))
        // Double tap in the middle: play or pause, no controls toggle.
        XCTAssertNil(taps.tap(x: 450, width: 900, at: 120))
        XCTAssertEqual(taps.tap(x: 450, width: 900, at: 120.2), .togglePlay)
        XCTAssertNil(taps.settle(at: 120 + wait))
    }

    func testTheTimingWheel() {
        XCTAssertEqual(SubtitleDelayWheel.values.count, 201)
        XCTAssertEqual(SubtitleDelayWheel.values.first, -100)
        XCTAssertEqual(SubtitleDelayWheel.values.last, 100)
        XCTAssertEqual(SubtitleDelayWheel.tenths(fromMs: 0), 0)
        XCTAssertEqual(SubtitleDelayWheel.tenths(fromMs: 500), 5)
        XCTAssertEqual(SubtitleDelayWheel.tenths(fromMs: -1250), -13)  // rounded
        XCTAssertEqual(SubtitleDelayWheel.tenths(fromMs: 1240), 12)
        XCTAssertEqual(SubtitleDelayWheel.tenths(fromMs: 25_000), 100)  // older ±0.5 s taps beyond 10 s
        XCTAssertEqual(SubtitleDelayWheel.tenths(fromMs: -25_000), -100)
        XCTAssertEqual(SubtitleDelayWheel.ms(fromTenths: 5), 500)
        XCTAssertEqual(SubtitleDelayWheel.ms(fromTenths: -37), -3700)
        XCTAssertEqual(SubtitleDelayWheel.ms(fromTenths: 500), 10_000)
        for value in SubtitleDelayWheel.values {
            XCTAssertEqual(SubtitleDelayWheel.tenths(fromMs: SubtitleDelayWheel.ms(fromTenths: value)), value)
        }
        XCTAssertEqual(SubtitleDelayWheel.label(tenths: 0), "0 s")
        XCTAssertEqual(SubtitleDelayWheel.label(tenths: 5), "+0.5 s")
        XCTAssertEqual(SubtitleDelayWheel.label(tenths: -12), "−1.2 s")
        XCTAssertEqual(SubtitleDelayWheel.label(tenths: 100), "+10.0 s")
    }
}
