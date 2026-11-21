import Foundation

// The iPhone/iPad video player's gestures (downloaded and streamed episodes alike): YouTube's
// taps on the video, pinch to zoom with pan, and the subtitle timing wheel.

/// Where a tap on the video lands: the left third goes back, the right third forward, the
/// middle pauses or plays (on a double tap).
public enum VideoTapZone: Equatable, Sendable {
    case back, middle, forward

    public static func at(x: Double, width: Double) -> VideoTapZone {
        guard width > 0, x.isFinite else { return .middle }
        let part = x / width
        if part < 1.0 / 3.0 { return .back }
        if part > 2.0 / 3.0 { return .forward }
        return .middle
    }
}

/// What a tap on the video does.
public enum VideoTapAction: Equatable, Sendable {
    /// One tap and no second one: show or hide the controls (as before).
    case toggleControls
    /// A double tap in the middle: pause or play.
    case togglePlay
    /// Seek by `deltaMs` now; `totalMs` is the whole series so far, shown as "+20 s".
    case seek(deltaMs: Int64, totalMs: Int64)
}

/// Taps on the video as on YouTube: a double tap on a side seeks 10 s, every further tap on
/// that side while the "+10 s" ripple shows adds 10 s more, a double tap in the middle pauses or
/// plays, and a lone tap toggles the controls once no second tap followed. Both taps of a double
/// tap must be in the same zone: a tap on the left and then one on the right are two single taps
/// (a tap on the other side ends a series and counts as a first tap there).
public struct VideoTaps: Equatable, Sendable {
    /// A second tap within this makes a double tap (seconds).
    public static let doubleTapInterval: TimeInterval = 0.3
    /// A series of seeks goes on with taps up to this long after its last one (seconds).
    public static let seriesWindow: TimeInterval = 0.7
    public static let stepMs: Int64 = 10_000

    private var pending: TimeInterval?
    private var pendingZone: VideoTapZone = .middle
    private var direction: Int64 = 0
    private var total: Int64 = 0
    private var last: TimeInterval = 0

    public init() {}

    /// A tap waits whether a second one comes (call `settle(at:)` `doubleTapInterval` later).
    public var waiting: Bool { pending != nil }

    /// A tap at `x` of a video `width` wide at `time` (seconds, monotonic). nil: wait whether a
    /// second tap comes; call `settle(at:)` `doubleTapInterval` later (also after an action
    /// while `waiting`: the first of two taps in different zones toggles the controls at once,
    /// the second waits as a first tap).
    public mutating func tap(x: Double, width: Double, at time: TimeInterval) -> VideoTapAction? {
        let zone = VideoTapZone.at(x: x, width: width)
        let side: Int64 = zone == .back ? -1 : zone == .forward ? 1 : 0
        if direction != 0, time - last <= Self.seriesWindow {
            if side == direction { return seek(side, at: time) }
            // Another zone: the series ends, this is a first tap there.
            direction = 0; total = 0
            wait(zone, at: time)
            return nil
        }
        if let first = pending, time - first <= Self.doubleTapInterval {
            guard zone == pendingZone else {
                // Left then right (or a side and the middle): two single taps.
                direction = 0; total = 0
                wait(zone, at: time)
                return .toggleControls
            }
            pending = nil
            guard side != 0 else { direction = 0; return .togglePlay }
            direction = side; total = 0
            return seek(side, at: time)
        }
        direction = 0; total = 0
        wait(zone, at: time)
        return nil
    }

    private mutating func wait(_ zone: VideoTapZone, at time: TimeInterval) {
        pending = time
        pendingZone = zone
    }

    /// The wait after a tap ended: a lone tap shows or hides the controls; nil when a second
    /// tap made it a double tap (or a newer tap waits).
    public mutating func settle(at time: TimeInterval) -> VideoTapAction? {
        guard let first = pending, time - first >= Self.doubleTapInterval - 0.02 else { return nil }
        pending = nil
        return .toggleControls
    }

    /// The series total on screen at `time` (ms), nil when no series runs.
    public func series(at time: TimeInterval) -> Int64? {
        direction != 0 && time - last <= Self.seriesWindow ? total : nil
    }

    private mutating func seek(_ side: Int64, at time: TimeInterval) -> VideoTapAction {
        pending = nil
        last = time
        total += side * Self.stepMs
        return .seek(deltaMs: side * Self.stepMs, totalMs: total)
    }

    /// "+20 s", "−10 s".
    public static func label(totalMs: Int64) -> String {
        (totalMs < 0 ? "−" : "+") + "\(abs(totalMs) / 1000) s"
    }
}

/// A width and height (points), for the zoom.
public struct ZoomSize: Equatable, Sendable {
    public let width: Double
    public let height: Double
    public init(width: Double, height: Double) { self.width = width; self.height = height }
}

/// Pinch to zoom the video (the subtitles stay as they are), pan while zoomed; pinching back
/// below 1× snaps to fit. The offset moves the zoomed picture from the middle of the view and
/// never shows more than the picture's edge.
public struct VideoZoom: Equatable, Sendable {
    public static let maxScale = 4.0
    /// Pinching below 1× follows the fingers down to this, then it snaps back on release.
    public static let minPinchScale = 0.7
    /// A pinch released below this ends at 1× (fit).
    public static let snapScale = 1.05

    private struct Start: Equatable, Sendable { let scale: Double; let x: Double; let y: Double }

    public private(set) var scale = 1.0
    public private(set) var offsetX = 0.0
    public private(set) var offsetY = 0.0
    private var pinchStart: Start?
    private var panStart: Start?

    public init() {}

    public var isZoomed: Bool { scale > 1.001 }

    /// A pinch: `magnification` since it began, around `focusX/focusY` (from the view's middle):
    /// the point under the fingers stays there.
    public mutating func pinch(_ magnification: Double, focusX: Double, focusY: Double, picture: ZoomSize, view: ZoomSize) {
        if pinchStart == nil { pinchStart = Start(scale: scale, x: offsetX, y: offsetY) }
        guard let begin = pinchStart, magnification.isFinite, magnification > 0, begin.scale > 0 else { return }
        let next = min(Self.maxScale, max(Self.minPinchScale, begin.scale * magnification))
        let ratio = next / begin.scale
        scale = next
        offsetX = focusX - (focusX - begin.x) * ratio
        offsetY = focusY - (focusY - begin.y) * ratio
        clamp(picture: picture, view: view)
    }

    public mutating func endPinch(picture: ZoomSize, view: ZoomSize) {
        pinchStart = nil
        if scale < Self.snapScale { reset() } else { clamp(picture: picture, view: view) }
    }

    /// A drag while zoomed: `dx/dy` since it began (ignored during a pinch and at 1×).
    public mutating func pan(dx: Double, dy: Double, picture: ZoomSize, view: ZoomSize) {
        guard pinchStart == nil, isZoomed, dx.isFinite, dy.isFinite else { return }
        if panStart == nil { panStart = Start(scale: scale, x: offsetX, y: offsetY) }
        guard let begin = panStart else { return }
        offsetX = begin.x + dx
        offsetY = begin.y + dy
        clamp(picture: picture, view: view)
    }

    public mutating func endPan() { panStart = nil }

    public mutating func reset() {
        scale = 1; offsetX = 0; offsetY = 0; pinchStart = nil; panStart = nil
    }

    /// How far the zoomed picture may move each way from the middle.
    public static func limit(scale: Double, picture: ZoomSize, view: ZoomSize) -> (x: Double, y: Double) {
        (max(0, (picture.width * scale - view.width) / 2), max(0, (picture.height * scale - view.height) / 2))
    }

    private mutating func clamp(picture: ZoomSize, view: ZoomSize) {
        let bound = Self.limit(scale: scale, picture: picture, view: view)
        offsetX = min(bound.x, max(-bound.x, offsetX))
        offsetY = min(bound.y, max(-bound.y, offsetY))
    }
}

/// What the player's UIKit recognizers report for the zoom (VideoGestureLayer in the app): a
/// pinch's scale since it began around where it began (from the view's middle), a one-finger
/// drag's translation since it began, and their ends.
public enum VideoZoomGesture: Equatable, Sendable {
    case pinch(magnification: Double, focusX: Double, focusY: Double)
    case pinchEnded
    case pan(dx: Double, dy: Double)
    case panEnded
}

extension VideoZoom {
    /// One report of the recognizers (see VideoZoomGesture).
    public mutating func apply(_ gesture: VideoZoomGesture, picture: ZoomSize, view: ZoomSize) {
        switch gesture {
        case let .pinch(magnification, focusX, focusY):
            pinch(magnification, focusX: focusX, focusY: focusY, picture: picture, view: view)
        case .pinchEnded:
            endPinch(picture: picture, view: view)
        case let .pan(dx, dy):
            pan(dx: dx, dy: dy, picture: picture, view: view)
        case .panEnded:
            endPan()
        }
    }
}

/// The subtitle timing wheel: −10.0…+10.0 s in 0.1 s steps (rows are tenths of a second);
/// + shows the subtitles later, − earlier. The delay itself is kept in milliseconds.
public enum SubtitleDelayWheel {
    public static let values = Array(-100...100)

    /// The row for a delay (rounded to 0.1 s; a delay beyond ±10 s shows at the end of the wheel).
    public static func tenths(fromMs ms: Int) -> Int {
        let rounded = (Double(ms) / 100).rounded()
        guard rounded.isFinite else { return 0 }
        return Int(min(100, max(-100, rounded)))
    }

    public static func ms(fromTenths value: Int) -> Int { min(100, max(-100, value)) * 100 }

    /// "+0.5 s", "−1.2 s", and "0 s" for in sync (the wheel's 0 mark).
    public static func label(tenths value: Int) -> String {
        guard value != 0 else { return "0 s" }
        let size = abs(value)
        return (value > 0 ? "+" : "−") + "\(size / 10).\(size % 10) s"
    }
}

/// Touches on a selected subtitle line versus the player's own gestures (VideoGestureLayer).
/// With text selected in the line, a touch on a selection handle's target, on the selected text,
/// or any touch while a handle (or a long press) is being dragged belongs to the selection: the
/// player's tap, pinch and pan leave it alone. Without a selection the player has every touch.
public enum SubtitleSelectionTouches {
    public static func playerYields(hasSelection: Bool, onHandle: Bool, onSelectedText: Bool, dragging: Bool) -> Bool {
        guard hasSelection else { return false }
        return dragging || onHandle || onSelectedText
    }
}

/// Which subtitle line the player shows: while text in a line is selected that line stays
/// (the clock may reach the next line or a gap, which would drop the selection and its handles
/// mid-drag); otherwise the line at the current time.
public enum SubtitleCueHold {
    /// `current`: the cue at the clock; `held`: the cue whose text is selected; `count`: the cues.
    public static func shown(current: Int?, held: Int?, count: Int) -> Int? {
        if let held, held >= 0, held < count { return held }
        guard let current, current >= 0, current < count else { return nil }
        return current
    }
}

/// A compact wheel (the player's subtitle timing and text size) and the value it stands for.
/// The wheel is UIKit's picker; while it turns on its own after a flick, the value it was set to
/// stays the one outside until it stops (the picker reports a row only then). A wheel that was
/// set to that value again on every redraw of the player (its clock ticks ten times a second)
/// stopped mid-flick on the old row: a fast flick went straight back to 0, a slow drag did not.
/// So the wheel is set from outside only when the value outside really changed (Reset, another
/// episode), never to the value it already shows or reported last.
public struct WheelSync: Equatable, Sendable {
    /// The row the wheel shows (set from outside or turned by the user); nil before the first value.
    public private(set) var shown: Int?

    public init() {}

    /// The value from outside, as a row: the row to turn the wheel to, nil to leave it alone.
    public mutating func external(row: Int) -> Int? {
        guard row != shown else { return nil }
        shown = row
        return row
    }

    /// The user turned the wheel to `row` (the picker reported it once it stopped).
    public mutating func picked(row: Int) { shown = row }

    /// The row of `value` among `values` (the nearest one; 0 without values).
    public static func row(of value: Int, in values: [Int]) -> Int {
        guard !values.isEmpty else { return 0 }
        if let exact = values.firstIndex(of: value) { return exact }
        var best = 0
        for (index, candidate) in values.enumerated() where abs(candidate - value) < abs(values[best] - value) { best = index }
        return best
    }

    /// The value at a row the picker reported, kept inside the wheel (nil without values).
    public static func value(atRow row: Int, in values: [Int]) -> Int? {
        guard !values.isEmpty else { return nil }
        return values[min(values.count - 1, max(0, row))]
    }
}

/// The player's subtitle text size, a wheel next to the timing wheel: 70…200 % in 10 % steps of
/// the size the subtitles have by default (JitenMPV's share of the picture height, or 24 points
/// without the Mac's look). Furigana is drawn at half the text's size, so it scales with it.
public enum SubtitleTextSize {
    public static let values = Array(stride(from: 70, through: 200, by: 10))
    public static let standard = 100
    public static let defaultsKey = "pudge.subtitleTextPercent"

    /// A stored or wanted percentage on the wheel (nearest step; 100 for nothing usable).
    public static func clamp(_ percent: Double?) -> Int {
        guard let percent, percent.isFinite, percent > 0 else { return standard }
        let step = Int((percent / 10).rounded()) * 10
        return min(values.last ?? 200, max(values.first ?? 70, step))
    }

    /// The first value from the earlier Larger/Smaller menu (a scale, 1 = 100 %), once.
    public static func migrated(scale: Double?) -> Int { clamp(scale.map { $0 * 100 }) }

    /// The font size at `percent` of the default `base` size (never below 12 points).
    public static func fontSize(base: Double, percent: Int) -> Double {
        let size = base * Double(clamp(Double(percent))) / 100
        return size.isFinite ? max(12, size) : max(12, base)
    }

    public static func label(_ percent: Int) -> String { "\(clamp(Double(percent))) %" }
}

/// The player's two subtitle wheels are two different settings: each has its own button in the
/// top bar and its own small panel, and only one panel is open at a time. Tapping a panel's
/// button again closes it; tapping the other button switches to that panel.
public enum PlayerWheelPanel: String, Equatable, Sendable, CaseIterable {
    /// Subtitle timing (this episode, −10…+10 s).
    case timing
    /// Subtitle text size (all episodes, this device).
    case size

    /// The panel open after tapping `panel`'s button while `open` is open (nil: none).
    public static func tapped(_ panel: PlayerWheelPanel, open: PlayerWheelPanel?) -> PlayerWheelPanel? {
        open == panel ? nil : panel
    }
}
