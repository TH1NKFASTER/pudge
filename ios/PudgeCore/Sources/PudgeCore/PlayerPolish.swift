import Foundation

// The iPhone/iPad player and app shell: the episode's end closes the player, a tab tapped again
// goes back to its first screen, the controls hide after a while, the subtitle line takes taps
// only on its text, and episodes download nearest to the next one to watch first.

/// The episode played to its end: the player saves the place and closes (as Close does).
public enum PlaybackEnd {
    /// Within this of the duration counts as the end (ms).
    public static let toleranceMs: Int64 = 1000

    /// `positionMs` at the end of `durationMs`, and playback stopped by itself (it was playing a
    /// moment ago and now is not). A pause by hand before the end never closes the player.
    public static func reached(positionMs: Int64, durationMs: Int64, playing: Bool, wasPlaying: Bool) -> Bool {
        guard durationMs > 0, positionMs >= durationMs - toleranceMs else { return false }
        return !playing && wasPlaying
    }

    /// AVPlayerItem's "played to its end" notice ends the episode only at its end (or when the
    /// duration is not known): a stream whose bytes stopped short posts it mid-episode.
    public static func noticeIsEnd(positionMs: Int64, durationMs: Int64) -> Bool {
        durationMs <= 0 || positionMs >= durationMs - toleranceMs
    }
}

/// A tab of the tab bar selected: another tab keeps its own inner screen; the tab already shown
/// tapped again goes back to its first screen (as in iOS apps).
public enum TabReselection: Equatable, Sendable {
    case switchTab
    case popToRoot

    public static func action<Tab: Equatable>(current: Tab, tapped: Tab) -> TabReselection {
        current == tapped ? .popToRoot : .switchTab
    }
}

/// The player's controls hide `delay` seconds after the last interaction (tap, drag, wheel,
/// button), also while paused; never while a menu, a panel, a sheet or a selection is open or a
/// drag (the position slider) goes on.
public enum PlayerChromeTimer {
    public static let delay: TimeInterval = 5

    public static func hides(shown: Bool, idleSeconds: TimeInterval, holding: Bool) -> Bool {
        shown && !holding && idleSeconds >= delay - 0.05
    }
}

/// A rectangle (points) of laid-out text, for the subtitle line's hit test.
public struct TextBox: Equatable, Sendable {
    public let x: Double
    public let y: Double
    public let width: Double
    public let height: Double
    public init(x: Double, y: Double, width: Double, height: Double) {
        self.x = x; self.y = y; self.width = width; self.height = height
    }
}

/// A touch on the subtitle line is the line's only on its text (each line's glyph box plus a
/// small slop); past the text it goes to the video (a tap shows the controls, two seek).
public enum SubtitleHitArea {
    public static let slop = 8.0

    public static func hits(x: Double, y: Double, boxes: [TextBox], slop: Double = SubtitleHitArea.slop) -> Bool {
        guard x.isFinite, y.isFinite else { return false }
        return boxes.contains { box in
            box.width > 0 && box.height > 0
                && x >= box.x - slop && x <= box.x + box.width + slop
                && y >= box.y - slop && y <= box.y + box.height + slop
        }
    }
}

/// Episodes of one anime waiting to download on this device: the one nearest to the next to
/// watch first (the first unwatched after the last watched; without a watched one the lowest
/// number), then on from it, the ones behind it (a download again, a rewatch) last, also
/// ascending: a rewatch starts at its first episode, not at the last watched one.
public enum EpisodeDownloadOrder {
    /// The next to watch among `episodes` (number, watched); nil without episodes.
    public static func nextToWatch(_ episodes: [(number: Int, watched: Bool)]) -> Int? {
        let sorted = episodes.sorted { $0.number < $1.number }
        guard let lowest = sorted.first?.number else { return nil }
        if let last = sorted.last(where: { $0.watched })?.number {
            return sorted.first { !$0.watched && $0.number > last }?.number ?? last + 1
        }
        return lowest
    }

    /// Download order: anime as they first appear in `items`, inside each nearest first.
    public static func arrange<Item>(_ items: [Item], mediaId: (Item) -> Int, number: (Item) -> Int,
                                     next: (Int) -> Int?) -> [Item] {
        var order: [Int] = []
        var groups: [Int: [Item]] = [:]
        for item in items {
            let key = mediaId(item)
            if groups[key] == nil { order.append(key) }
            groups[key, default: []].append(item)
        }
        return order.flatMap { key -> [Item] in
            let members = groups[key] ?? []
            guard let target = next(key) else { return members.sorted { number($0) < number($1) } }
            return members.sorted { left, right in
                rank(number(left), target: target) < rank(number(right), target: target)
            }
        }
    }

    private static func rank(_ value: Int, target: Int) -> (Int, Int) {
        (value < target ? 1 : 0, value)
    }
}
