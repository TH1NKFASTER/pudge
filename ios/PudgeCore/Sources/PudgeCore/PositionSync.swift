import Foundation

/// When the place of an episode playing on this device goes to the Mac, so it can be continued
/// there (mpv opens at it): every `period` seconds while it plays, at once on pause and after a
/// seek, and always when the player closes or the app goes to the background. A place the Mac
/// cannot take now waits on this device (the episode's record) and goes once it answers.
public struct PositionSyncSchedule: Sendable {
    public enum Event: Equatable, Sendable { case paused, closed, background }

    /// Seconds between two sends while playing.
    public let period: TimeInterval
    /// A jump of the clock this much beyond what playing could do is a seek (seconds).
    public let seekJump: Double
    /// Seeks (scrubbing) send at most this often (seconds).
    public let seekGap: TimeInterval
    public private(set) var lastSentAt: TimeInterval?
    private var lastSample: (positionMs: Int64, at: TimeInterval)?

    public init(period: TimeInterval = 15, seekJump: Double = 5, seekGap: TimeInterval = 2) {
        self.period = period; self.seekJump = seekJump; self.seekGap = seekGap
    }

    /// A new player time: true when the place goes to the Mac now (then call `sent(at:)`).
    public mutating func observe(positionMs: Int64, at now: TimeInterval, playing: Bool) -> Bool {
        let previous = lastSample
        lastSample = (positionMs, now)
        if let previous {
            let moved = Double(positionMs - previous.positionMs) / 1000
            let passed = max(0, now - previous.at)
            // Back, or further on than playing (up to 2×) could go: a seek. A pause is not one
            // (the clock stood still while time passed).
            if moved < -seekJump || moved > passed * 2 + seekJump {
                return lastSentAt.map { now - $0 >= seekGap } ?? true
            }
        }
        guard playing else { return false }
        guard let lastSentAt else {
            // The first send comes one period after the player opened (opening is no news).
            self.lastSentAt = now
            return false
        }
        return now - lastSentAt >= period
    }

    /// Pause, close or background: the place goes now (the next periodic send counts from here).
    public mutating func event(_ event: Event, at now: TimeInterval) -> Bool {
        lastSentAt = now
        return true
    }

    public mutating func sent(at now: TimeInterval) { lastSentAt = now }
}

/// The seconds played on this device as `[[start, end], …]` (seconds) for the Mac, which sums the
/// parts watched on every device (overlaps once). At most `limit` parts: the longest ones.
public enum WatchedRanges {
    public static func ranges(_ seconds: IndexSet, limit: Int = 200) -> [[Double]] {
        var parts = seconds.rangeView.map { [Double($0.lowerBound), Double($0.upperBound)] }
        if parts.count > limit {
            parts = Array(parts.sorted { ($0[1] - $0[0]) > ($1[1] - $1[0]) }.prefix(limit))
                .sorted { $0[0] < $1[0] }
        }
        return parts
    }
}
