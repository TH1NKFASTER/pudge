import Foundation

/// When a watched episode downloaded on this iPhone/iPad is deleted, as the Mac deletes its
/// own copies (`agent.delete_after_watched_hours`, sent with the episode list as
/// `deleteWatchedAfterHours`). Settings → Downloads can choose otherwise for this device.
public enum WatchedCleanupChoice: String, CaseIterable, Sendable {
    case mac, immediately, day, week, off
    public static let defaultsKey = "pudge.deleteWatched"
    /// A missing or unknown stored value: the Mac's setting.
    public init(stored: String?) { self = stored.flatMap(Self.init(rawValue:)) ?? .mac }

    /// Seconds from the counted watch to the deletion; nil: never deleted automatically.
    /// `macHours`: the Mac's setting (nil: an older Mac that does not send it, nothing is deleted).
    public func delay(macHours: Double?) -> TimeInterval? {
        switch self {
        case .off: return nil
        case .immediately: return 0
        case .day: return 86_400
        case .week: return 7 * 86_400
        case .mac:
            guard let macHours, macHours.isFinite else { return nil }
            return max(0, macHours) * 3600
        }
    }
}

/// What the automatic deletion of one downloaded episode depends on.
public struct WatchedCleanupState: Equatable, Sendable {
    /// When the watch counted on this device (seconds); nil: not watched here.
    public var watchedAt: Double?
    /// "Keep" was tapped, or the episode was downloaded again after it had been watched.
    public var kept: Bool
    public var positionMs: Int64
    public var durationMs: Int64
    /// Open in the player now, or being downloaded again.
    public var busy: Bool
    /// Watched on the Mac or another device (MacWatchedEpisodes): the copy here starts from the
    /// beginning, so its place says nothing about a watch; only a place moved here since (opened
    /// again) means it is being watched again.
    public var watchedElsewhere: Bool
    public init(watchedAt: Double?, kept: Bool, positionMs: Int64, durationMs: Int64, busy: Bool = false,
                watchedElsewhere: Bool = false) {
        self.watchedAt = watchedAt; self.kept = kept; self.positionMs = positionMs; self.durationMs = durationMs; self.busy = busy
        self.watchedElsewhere = watchedElsewhere
    }
}

public enum WatchedCleanup {
    /// Below this share of the episode it is being watched (again): never deleted.
    public static let watchingBelow = 0.85

    /// When the download goes (seconds since 1970); nil: it stays.
    public static func deleteAt(_ state: WatchedCleanupState, delay: TimeInterval?) -> Double? {
        guard let delay, delay.isFinite, let watchedAt = state.watchedAt, watchedAt.isFinite, !state.kept,
              state.durationMs > 0 else { return nil }
        // Watched elsewhere and not opened here since: its place stays at the start (it was never
        // deleted: under 85 % read as "being watched again").
        let untouched = state.watchedElsewhere && state.positionMs == 0
        guard untouched || Double(state.positionMs) >= Double(state.durationMs) * watchingBelow else { return nil }
        return watchedAt + max(0, delay)
    }

    /// Delete it now? Never while it plays or downloads.
    public static func isDue(_ state: WatchedCleanupState, delay: TimeInterval?, now: Double) -> Bool {
        guard !state.busy, let at = deleteAt(state, delay: delay) else { return false }
        return now >= at
    }

    /// "Deleting in 5 h" for the list (rounded up: never "in 0 h" while time is left).
    public static func label(deleteAt: Double, now: Double) -> String {
        let left = deleteAt - now
        guard left > 60 else { return "Deleting soon" }
        if left < 3600 { return "Deleting in \(Int((left / 60).rounded(.up))) min" }
        if left < 48 * 3600 { return "Deleting in \(Int((left / 3600).rounded(.up))) h" }
        return "Deleting in \(Int((left / 86_400).rounded(.up))) d"
    }
}
